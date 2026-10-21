"""ComfyUI 手机控制台 —— 让手机浏览器直接跑你自己的工作流。

只做一件事：把你的 API 格式工作流当成模板，替换掉你填的几个字段，
原样提交给 ComfyUI 后端。所以自定义节点一个都不会丢。
"""
import copy
import ipaddress
import json
import mimetypes
import pathlib
import random
import shutil
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    Image = None
    HAS_PIL = False

sys.stdout.reconfigure(encoding="utf-8")

if getattr(sys, "frozen", False):
    # 打包成 exe 后：数据放 exe 旁边，静态资源在解包目录里
    ROOT = pathlib.Path(sys.executable).resolve().parent
    RES_DIR = pathlib.Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = pathlib.Path(__file__).resolve().parent
    RES_DIR = ROOT

API_DIR = ROOT / "api_workflows"
WEB_DIR = RES_DIR / "web"
CONFIG_FILE = ROOT / "config.json"

DEFAULT_CONFIG = {
    "comfy_url": "http://127.0.0.1:8188",
    "port": 8899,
    "bind": "0.0.0.0",
    "open_admin": True,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if not CONFIG_FILE.exists():
        try:
            CONFIG_FILE.write_text(
                json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        return cfg
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    except Exception as e:
        print(f"[警告] config.json 读取失败，使用默认配置：{e}")
    return cfg


CFG = load_config()
COMFY = str(CFG["comfy_url"]).rstrip("/")
PORT = int(CFG["port"])
BIND = str(CFG.get("bind") or "0.0.0.0")

# 直连 ComfyUI，绕开系统代理（Clash 等会把 127.0.0.1 也劫持成 502）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_cache = {}
_cache_lock = threading.Lock()
_info_cache = {}

TEXT_KEYS = {"text", "prompt", "string", "text_g", "text_l", "positive", "negative"}
NUM_KEYS = {
    "seed", "noise_seed", "steps", "cfg", "width", "height", "batch_size",
    "denoise", "strength", "scale_by", "megapixels", "length", "fps", "frame_count",
}
SKIP_CLASS = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode"}


def local_addresses():
    """列出本机可用于手机访问的地址，供界面显示，避免写死某个 IP。"""
    lan, tail = [], []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            lan.append(ip)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip == "0.0.0.0" or ip in lan or ip in tail:
                continue
            (tail if ip.startswith("100.") else lan).append(ip)
    except Exception:
        pass
    return {"lan": lan, "tailscale": tail, "port": PORT}


def comfy_get(path, timeout=8):
    with _opener.open(COMFY + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def comfy_bytes(path, timeout=60):
    with _opener.open(COMFY + path, timeout=timeout) as r:
        return r.read()


def compose_mask(base_name, mask_name):
    """在服务端合成「原图 RGB + 遮罩 alpha」的 PNG。

    浏览器 canvas 导出 PNG 时会把透明像素的 RGB 清零，导致 ComfyUI 转 RGB 后
    那块变成纯黑/纯白；服务端用 Pillow 合成可以保证 RGB 完整，
    只把遮罩区的 alpha 压到 1（几乎透明但非零，ComfyUI 的 mask = 1 - alpha ≈ 0.996）。
    """
    if not HAS_PIL:
        raise ValueError("服务端缺少 Pillow，无法合成遮罩（pip install pillow）")
    q = urllib.parse.quote
    base = Image.open(
        BytesIO(comfy_bytes(f"/view?filename={q(base_name)}&type=input"))
    ).convert("RGB")
    mask = Image.open(
        BytesIO(comfy_bytes(f"/view?filename={q(mask_name)}&type=input"))
    ).convert("L")
    if mask.size != base.size:
        mask = mask.resize(base.size, Image.LANCZOS)

    alpha = mask.point(lambda v: 1 if v > 108 else 255)
    out = base.copy()
    out.putalpha(alpha)
    buf = BytesIO()
    out.save(buf, "PNG")

    name = base_name.rsplit(".", 1)[0] + "_masked.png"
    res = upload_image(buf.getvalue(), name)
    return res.get("name", name)


def comfy_post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        COMFY + path, data=data, headers={"Content-Type": "application/json"}
    )
    with _opener.open(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


INFO_CACHE_FILE = ROOT / ".cache" / "object_info.json"
_info_cache = {}
_failed_types = set()
_comfy_check = {"ok": None, "ts": 0}


def _load_info_cache():
    global _info_cache
    try:
        _info_cache = json.loads(INFO_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        _info_cache = {}


def _save_info_cache():
    try:
        INFO_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        INFO_CACHE_FILE.write_text(
            json.dumps(_info_cache, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def comfy_available():
    """轻量探测 ComfyUI 是否在线，结果缓存 15 秒。"""
    now = time.time()
    if _comfy_check["ok"] is not None and now - _comfy_check["ts"] < 15:
        return _comfy_check["ok"]
    try:
        with _opener.open(COMFY + "/system_stats", timeout=2) as r:
            r.read(1)
        ok = True
    except Exception:
        ok = False
    _comfy_check["ok"] = ok
    _comfy_check["ts"] = now
    return ok


def object_info(class_type):
    with _cache_lock:
        hit = _info_cache.get(class_type)
    if hit is not None:
        return hit
    if class_type in _failed_types or not comfy_available():
        return {}
    try:
        info = comfy_get(f"/object_info/{urllib.parse.quote(class_type)}")
        info = info.get(class_type, {})
    except Exception:
        with _cache_lock:
            _failed_types.add(class_type)
        return {}
    with _cache_lock:
        if info:
            _info_cache[class_type] = info
            _save_info_cache()
    return info


def wf_path(wf_id):
    p = (API_DIR / urllib.parse.unquote(wf_id)).resolve()
    if not str(p).startswith(str(API_DIR.resolve())) or not p.exists():
        raise FileNotFoundError(wf_id)
    return p


def list_workflows():
    """所有工作流平铺在 api_workflows 里，分类与顺序全部来自各自的 .ui.json。"""
    out = []
    for f in sorted(API_DIR.glob("*.api.json")):
        ui = load_ui_override(f)
        name = ui.get("title") or f.name[: -len(".api.json")]
        cat = (ui.get("category") or "").strip() or "未分类"
        try:
            order = float(ui.get("order", 0))
        except (TypeError, ValueError):
            order = 0
        out.append({"id": f.name, "name": name, "category": cat, "order": order})
    out.sort(key=lambda x: (x["order"], x["id"]))
    return out


def _renumber(items):
    for i, it in enumerate(items):
        p = wf_path(it["id"])
        ui = load_ui_override(p)
        ui["order"] = (i + 1) * 10
        write_ui(p, ui)


def _is_link(v):
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)


def extract_params(prompt):
    """从 API prompt 里挑出人能调的字段。

    难点：有的工作流用一个节点同时输出正向和负向（如 TextEncodeQwenImage21），
    此时正负要向靠 input 名字区分，不能靠节点判断。
    """
    pos_ids, neg_ids = set(), set()
    for nid, node in prompt.items():
        ins = node.get("inputs") or {}
        for k in ("positive", "negative"):
            v = ins.get(k)
            if _is_link(v):
                (pos_ids if k == "positive" else neg_ids).add(v[0])

    primary, advanced = [], []
    for nid, node in prompt.items():
        ct = node.get("class_type", "")
        if ct in SKIP_CLASS:
            continue
        title = (node.get("_meta") or {}).get("title") or ct
        inputs = node.get("inputs") or {}
        info = object_info(ct)
        req = (info.get("input") or {}).get("required") or {}
        opt = (info.get("input") or {}).get("optional") or {}

        for key, val in inputs.items():
            if _is_link(val) or key.startswith("_"):
                continue
            spec = req.get(key) or opt.get(key)
            spec_meta = {}
            spec_type = None
            if isinstance(spec, list) and spec:
                if isinstance(spec[0], str):
                    spec_type = spec[0].upper()
                if len(spec) > 1 and isinstance(spec[1], dict):
                    spec_meta = spec[1]

            choices = None
            if isinstance(spec, list) and spec:
                head = spec[0]
                if isinstance(head, list):
                    # 老格式: [["a","b"], {...}]
                    choices = head
                elif spec_type == "COMBO":
                    # 新格式: ["COMBO", {"options": [...]}]
                    choices = spec_meta.get("options")
                if choices:
                    choices = [c for c in choices if isinstance(c, (str, int, float))]

            kind = None
            if key == "image" and isinstance(val, str):
                kind, val = "image", ""
            elif choices and len(choices) > 1:
                kind = "select"
            elif spec_type in ("INT", "FLOAT"):
                kind = "number"
                try:
                    val = int(float(val)) if spec_type == "INT" else float(val)
                except (TypeError, ValueError):
                    pass
            elif isinstance(val, bool):
                kind = "toggle"
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                kind = "number"
            elif isinstance(val, str):
                kind = "text"
            if not kind:
                continue
            if kind == "text" and len(val) > 1500:
                continue

            low = key.lower()
            node_pos, node_neg = nid in pos_ids, nid in neg_ids
            if node_neg and not node_pos:
                is_neg = True
            else:
                is_neg = "neg" in low or "negative" in low

            item = {
                "key": f"{nid}.{key}",
                "node": nid,
                "input": key,
                "label": spec_meta.get("display_name") or f"{title} · {key}",
                "kind": kind,
                "value": val,
                "_title": title,
            }
            if choices and kind != "image":
                item["choices"] = choices[:200]
            if spec_meta.get("multiline"):
                item["multiline"] = True

            if kind == "text" and not is_neg and (node_pos or low in ("text", "prompt")):
                item["label"] = "正向提示词" if not primary or all(
                    p["kind"] != "text" or p["group"] != "prompt" for p in primary
                ) else f"正向提示词 · {title}"
                item["group"] = "prompt"
                primary.append(item)
            elif kind == "text" and is_neg:
                item["label"] = "负面提示词"
                item["group"] = "negative"
                advanced.append(item)
            elif kind == "image":
                item["label"] = f"{title} · 图片"
                item["group"] = "image"
                primary.append(item)
            elif low in ("seed", "noise_seed"):
                item["label"] = "种子"
                item["group"] = "seed"
                primary.append(item)
            elif low in (
                "width", "height", "batch_size", "aspect_ratio", "megapixels",
            ):
                item["label"] = {
                    "width": "宽度", "height": "高度", "batch_size": "张数",
                    "aspect_ratio": "画面比例", "megapixels": "像素量(MP)",
                }.get(low, low)
                item["group"] = "size"
                primary.append(item)
            else:
                item["group"] = "advanced"
                advanced.append(item)

    order = {"prompt": 0, "image": 1, "size": 2, "seed": 3}
    primary.sort(key=lambda x: order.get(x["group"], 9))

    imgs = [p for p in primary if p["group"] == "image"]
    for i, p in enumerate(imgs):
        p["label"] = f"参考图 {i + 1}" if len(imgs) > 1 else "参考图"
    prompts = [p for p in primary if p["group"] == "prompt"]
    for i, p in enumerate(prompts):
        if i:
            p["label"] = f"提示词 · {p['_title']}"
    seeds = [p for p in primary if p["group"] == "seed"]
    if len(seeds) > 1:
        for p in seeds:
            p["label"] = f"种子 · {p['_title']}"
    for p in primary + advanced:
        p.pop("_title", None)
    return {"primary": primary, "advanced": advanced}


def load_ui_override(p):
    """每个工作流可选的界面配置：<名字>.ui.json

    {"title": "MJ 古风人像", "fields": ["4.text", "7.aspect_ratio"],
     "labels": {"4.text": "提示词"}}
    """
    ui = p.with_name(p.name[: -len(".api.json")] + ".ui.json")
    if ui.exists():
        try:
            return json.loads(ui.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def workflow_detail(wf_id):
    p = wf_path(wf_id)
    ui_file = p.with_name(p.name[: -len(".api.json")] + ".ui.json")
    stamp = (
        str(p),
        p.stat().st_mtime,
        ui_file.stat().st_mtime if ui_file.exists() else 0,
    )
    with _cache_lock:
        hit = _cache.get(stamp)
        if hit:
            return hit[1]
    prompt = json.loads(p.read_text(encoding="utf-8"))
    params = extract_params(prompt)
    ui = load_ui_override(p)

    if ui.get("fields"):
        index = {it["key"]: it for it in params["primary"] + params["advanced"]}
        labels = ui.get("labels") or {}
        prim = []
        for k in ui["fields"]:
            it = index.pop(k, None)
            if not it:
                continue
            if k in labels:
                it["label"] = labels[k]
            prim.append(it)
        params = {"primary": prim, "advanced": list(index.values())}

    if ui.get("hideAdvanced"):
        params["advanced"] = []

    defaults = ui.get("defaults") or {}
    if defaults:
        for it in params["primary"] + params["advanced"]:
            if it["key"] in defaults:
                it["value"] = defaults[it["key"]]

    data = {
        "id": wf_id,
        "title": ui.get("title") or wf_id.split("/")[-1].replace(".api.json", ""),
        "prompt": prompt,
        "params": params,
    }
    with _cache_lock:
        _cache[stamp] = (0, data)
    return data


def delete_workflow(wf_id):
    """彻底删除工作流及其界面配置。"""
    p = wf_path(wf_id)
    ui = ui_path_of(p)
    p.unlink()
    if ui.exists():
        ui.unlink()
    with _cache_lock:
        for k in [k for k in _cache if k[0] == str(p)]:
            _cache.pop(k, None)
    return {"ok": True, "deleted": wf_id}


def ui_path_of(p):
    return p.with_name(p.name[: -len(".api.json")] + ".ui.json")


def write_ui(p, ui):
    out = {
        "title": ui.get("title") or p.name[: -len(".api.json")],
        "order": ui.get("order", 0),
        "hideAdvanced": bool(ui.get("hideAdvanced", True)),
        "fields": ui.get("fields") or [],
        "labels": {k: v for k, v in (ui.get("labels") or {}).items() if v},
        "defaults": ui.get("defaults") or {},
    }
    if ui.get("category"):
        out["category"] = ui["category"]
    ui_path_of(p).write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return out


def admin_detail(wf_id):
    p = wf_path(wf_id)
    prompt = json.loads(p.read_text(encoding="utf-8"))
    params = extract_params(prompt)
    ui = load_ui_override(p)
    selected = ui.get("fields") or []
    labels = ui.get("labels") or {}
    defaults = ui.get("defaults") or {}
    order_map = {k: i for i, k in enumerate(selected)}

    fields = []
    for it in params["primary"] + params["advanced"]:
        k = it["key"]
        fields.append(
            {
                "key": k,
                "label": labels.get(k) or it["label"],
                "kind": it["kind"],
                "value": defaults.get(k, it["value"]),
                "selected": k in selected,
                "multi": bool(it.get("multiline")),
                "choices": it.get("choices") or [],
            }
        )
    fields.sort(key=lambda f: (0, order_map[f["key"]]) if f["key"] in order_map else (1, 0))

    return {
        "id": wf_id,
        "title": ui.get("title") or p.name[: -len(".api.json")],
        "category": ui.get("category") or "",
        "order": ui.get("order", 0),
        "hideAdvanced": bool(ui.get("hideAdvanced", True)),
        "fields": fields,
    }


def save_ui(wf_id, body):
    p = wf_path(wf_id)
    prompt = json.loads(p.read_text(encoding="utf-8"))
    raw = {
        it["key"]: it["value"]
        for it in sum(extract_params(prompt).values(), [])
    }
    picked = [f for f in (body.get("fields") or []) if f.get("selected")]
    labels = {}
    defaults = {}
    for f in picked:
        k = f["key"]
        if f.get("label"):
            labels[k] = f["label"].strip()
        dv = f.get("default")
        if dv is None or str(dv).strip() == "":
            continue
        kind = f.get("kind")
        if kind == "toggle" and isinstance(dv, str):
            dv = dv.strip().lower() in ("true", "1", "yes", "on")
        elif kind == "number" and isinstance(dv, str):
            try:
                fv = float(dv)
                dv = int(fv) if fv == int(fv) else fv
            except (TypeError, ValueError):
                pass
        if k in raw and str(dv) == str(raw[k]):
            continue
        defaults[k] = dv
    ui = {
        "title": (body.get("title") or "").strip() or p.name[: -len(".api.json")],
        "category": (body.get("category") or "").strip(),
        "order": body.get("order", 0),
        "hideAdvanced": True,
        "fields": [f["key"] for f in picked],
        "labels": labels,
        "defaults": defaults,
    }
    return write_ui(p, ui)


def reorder_workflow(wf_id, direction):
    items = list_workflows()
    idx = next((i for i, it in enumerate(items) if it["id"] == wf_id), None)
    if idx is None:
        raise ValueError("找不到该工作流")
    j = idx - 1 if direction == "up" else idx + 1
    if j < 0 or j >= len(items):
        return {"moved": False}
    items[idx], items[j] = items[j], items[idx]
    _renumber(items)
    return {"moved": True}


def move_group(name, direction):
    """调整分组之间的先后顺序（整组移动）。"""
    items = list_workflows()
    groups = []
    for it in items:
        if it["category"] not in groups:
            groups.append(it["category"])
    if name not in groups:
        raise ValueError("找不到该分组")
    idx = groups.index(name)
    j = idx - 1 if direction == "up" else idx + 1
    if j < 0 or j >= len(groups):
        return {"moved": False}
    groups[idx], groups[j] = groups[j], groups[idx]
    by_group = {g: [it for it in items if it["category"] == g] for g in groups}
    ordered = []
    for g in groups:
        ordered.extend(by_group[g])
    _renumber(ordered)
    return {"moved": True, "groups": groups}


def import_workflow(raw, filename, category):
    try:
        if isinstance(raw, (bytes, str)):
            data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        else:
            data = raw
    except Exception as e:
        raise ValueError(f"不是合法的 JSON：{e}")
    if not isinstance(data, dict) or not data:
        raise ValueError("文件内容是空的")
    for v in data.values():
        if not isinstance(v, dict) or "class_type" not in v:
            raise ValueError("这不是 API 格式工作流。请在 ComfyUI 里用「导出(API)」再上传。")

    name = pathlib.Path(filename or "workflow.json").name
    name = name[:-5] if name.lower().endswith(".json") else name
    name = name.replace(".api", "").strip() or "workflow"
    cat = (category or "").strip()

    dest = API_DIR / f"{name}.api.json"
    i = 2
    while dest.exists():
        dest = API_DIR / f"{name} ({i}).api.json"
        i += 1
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    existing = [it["order"] for it in list_workflows()]
    write_ui(dest, {"order": (max(existing) + 10) if existing else 10, "category": cat})
    return dest.name


def is_admin_client(ip):
    """管理页只允许本机与局域网，Tailscale 网段(手机)不可访问。"""
    try:
        addr = ipaddress.ip_address(ip.replace("::ffff:", ""))
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    if addr.version == 6:
        return False
    if addr in ipaddress.ip_network("100.64.0.0/10"):
        return False
    return addr.is_private


def _rand_seed(spec):
    """按节点声明的 min/max 取随机种子，避免超出节点允许范围。"""
    meta = {}
    if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict):
        meta = spec[1]

    def as_int(v, fallback):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return fallback

    lo = as_int(meta.get("min"), 0)
    hi = as_int(meta.get("max"), 2**53)
    hi = min(hi, 2**53)
    if hi <= lo:
        lo, hi = 0, 2**53
    return random.randint(lo, hi)


def build_prompt(wf_id, values):
    prompt = copy.deepcopy(workflow_detail(wf_id)["prompt"])
    values = values or {}

    # 种子自动随机：界面上没暴露的 seed 字段每次运行都换新值，避免重复出同一张图
    for nid, node in prompt.items():
        inputs = node.get("inputs") or {}
        if not any(k in inputs for k in ("seed", "noise_seed")):
            continue
        info = object_info(node.get("class_type", ""))
        req = (info.get("input") or {}).get("required") or {}
        opt = (info.get("input") or {}).get("optional") or {}
        for key in ("seed", "noise_seed"):
            if key not in inputs:
                continue
            if f"{nid}.{key}" in values:
                continue
            inputs[key] = _rand_seed(req.get(key) or opt.get(key))

    for key, val in values.items():
        nid, _, inp = key.partition(".")
        node = prompt.get(nid)
        if not node:
            continue
        ct = node.get("class_type", "")
        info = object_info(ct)
        spec = ((info.get("input") or {}).get("required") or {}).get(inp) or (
            (info.get("input") or {}).get("optional") or {}
        ).get(inp)
        stype = (
            spec[0].upper()
            if isinstance(spec, list) and spec and isinstance(spec[0], str)
            else None
        )
        cur = node["inputs"].get(inp)
        if stype == "BOOLEAN" or (stype is None and isinstance(cur, bool)):
            val = bool(val)
        elif stype in ("INT", "FLOAT") or (
            stype is None and isinstance(cur, (int, float)) and not isinstance(cur, bool)
        ):
            try:
                if stype == "FLOAT" or isinstance(cur, float):
                    val = float(val)
                else:
                    val = int(float(val))
            except (TypeError, ValueError):
                continue
        node["inputs"][inp] = val
    return prompt


def upload_image(raw, filename):
    boundary = "----comfy" + uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n" + raw + b"\r\n"
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n".encode()
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        COMFY + "/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with _opener.open(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                f = WEB_DIR / "index.html"
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/admin":
                f = WEB_DIR / "admin.html"
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            if u.path.startswith("/api/admin/"):
                if not is_admin_client(self.client_address[0]):
                    return self._err(403, "管理接口只允许在本机访问")
                if u.path == "/api/admin/list":
                    return self._send(200, {"items": list_workflows()})
                if u.path == "/api/admin/detail":
                    return self._send(200, admin_detail(q.get("id", [""])[0]))
            if u.path == "/api/info":
                return self._send(
                    200, {"addresses": local_addresses(), "comfy": COMFY, "port": PORT}
                )
            if u.path == "/api/workflows":
                return self._send(200, {"items": list_workflows()})
            if u.path.startswith("/api/workflow/"):
                return self._send(200, workflow_detail(u.path[len("/api/workflow/"):]))
            if u.path == "/api/job":
                pid = q.get("id", [""])[0]
                return self._send(200, comfy_get(f"/history/{pid}"))
            if u.path == "/api/view":
                fn = q.get("filename", [""])[0]
                sub = q.get("subfolder", [""])[0]
                url = f"{COMFY}/view?" + urllib.parse.urlencode(
                    {"filename": fn, "subfolder": sub, "type": q.get("type", ["output"])[0]}
                )
                with _opener.open(url, timeout=120) as r:
                    data = r.read()
                    ct = r.headers.get("Content-Type") or mimetypes.guess_type(fn)[0] or "image/png"
                return self._send(200, data, ct)
            if u.path == "/api/health":
                return self._send(200, comfy_get("/system_stats"))
            return self._err(404, "not found")
        except Exception as e:
            return self._err(500, str(e))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if u.path.startswith("/api/admin/"):
                if not is_admin_client(self.client_address[0]):
                    return self._err(403, "管理接口只允许在本机访问")
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if u.path == "/api/admin/save":
                    return self._send(200, {"ok": True, "ui": save_ui(body["id"], body)})
                if u.path == "/api/admin/reorder":
                    return self._send(
                        200, reorder_workflow(body["id"], body.get("dir", "up"))
                    )
                if u.path == "/api/admin/group-move":
                    return self._send(
                        200, move_group(body.get("name", ""), body.get("dir", "up"))
                    )
                if u.path == "/api/admin/import":
                    wid = import_workflow(
                        body.get("data"), body.get("filename"), body.get("category")
                    )
                    return self._send(200, {"ok": True, "id": wid})
                if u.path == "/api/admin/delete":
                    return self._send(200, delete_workflow(body.get("id", "")))
                return self._err(404, "not found")
            if u.path == "/api/run":
                body = json.loads(raw.decode("utf-8"))
                values = body.get("values") or {}
                for k, v in list(values.items()):
                    if isinstance(v, str) and v.startswith("upload:"):
                        self._err(400, "图片未上传")
                        return
                prompt = build_prompt(body["id"], values)
                res = comfy_post("/prompt", {"prompt": prompt, "client_id": uuid.uuid4().hex})
                return self._send(200, res)
            if u.path == "/api/mask":
                body = json.loads(raw.decode("utf-8")) if raw else {}
                name = compose_mask(body.get("base", ""), body.get("mask", ""))
                return self._send(200, {"name": name})
            if u.path == "/api/upload":
                fname = urllib.parse.unquote(self.headers.get("X-Filename") or "upload.png")
                return self._send(200, upload_image(raw, fname))
            if u.path == "/api/interrupt":
                return self._send(200, comfy_post("/interrupt", {}))
            return self._err(404, "not found")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:600]
            return self._err(400, f"ComfyUI 拒绝执行: {detail}")
        except Exception as e:
            return self._err(500, str(e))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    API_DIR.mkdir(parents=True, exist_ok=True)
    _load_info_cache()
    try:
        srv = Server((BIND, PORT), Handler)
    except OSError as e:
        print(f"[错误] 端口 {PORT} 启动失败：{e}")
        print("可能已被占用。改 config.json 里的 port 再试。")
        sys.exit(1)

    addr = local_addresses()
    print("")
    print("  ComfyUI 手机控制台")
    print("  " + "-" * 46)
    print(f"  ComfyUI 后端 : {COMFY}")
    print(f"  工作流目录   : {API_DIR}")
    print(f"  本机管理页   : http://127.0.0.1:{PORT}/admin")
    for ip in addr["lan"]:
        print(f"  局域网访问   : http://{ip}:{PORT}/")
    for ip in addr["tailscale"]:
        print(f"  外网(Tailscale): http://{ip}:{PORT}/")
    if not addr["lan"] and not addr["tailscale"]:
        print("  未检测到可用网卡地址，仅本机可访问")
    print("  " + "-" * 46)
    print("  关闭本窗口即停止服务")
    print("")
    if CFG.get("open_admin", True):
        threading.Timer(
            1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}/admin")
        ).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

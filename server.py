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
import re
import secrets
import socket
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

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

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
    # 访问令牌：null = 首次运行自动生成；"" = 关闭鉴权（只在自己完全信任的网络里这么干）
    "token": None,
    "verbose": False,
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


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[警告] config.json 写入失败：{e}")


CFG = load_config()
COMFY = str(CFG["comfy_url"]).rstrip("/")
PORT = int(CFG["port"])
BIND = str(CFG.get("bind") or "0.0.0.0")

# 首次运行（或旧版配置没有 token 字段）时生成一个随机令牌并写回配置
if CFG.get("token") is None:
    CFG["token"] = secrets.token_urlsafe(9)
    save_config(CFG)
TOKEN = str(CFG.get("token") or "").strip()
VERBOSE = bool(CFG.get("verbose"))

# 直连 ComfyUI，绕开系统代理（Clash 等会把 127.0.0.1 也劫持成 502）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_cache = {}
_cache_lock = threading.Lock()
_info_cache = {}

SKIP_CLASS = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode"}

MAX_UPLOAD = 64 * 1024 * 1024  # 单次上传上限，防止被塞爆内存
IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff"}
VIDEO_EXT = {"mp4", "webm", "mov", "mkv", "avi"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT
SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._\-()\[\] ]+")
IMAGE_KEYS = {
    "image", "img", "input_image", "source_image", "ref_image",
    "reference_image", "image_a", "image_b", "face_image", "mask_image",
}


class ApiError(Exception):
    """带 HTTP 状态码的业务错误，直接把信息回给前端。"""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def safe_filename(name, default="upload.png"):
    """只保留文件名本身，去掉路径，过滤掉奇怪字符与不允许的扩展名。"""
    raw = urllib.parse.unquote(str(name or ""))
    base = pathlib.PurePath(raw.replace("\\", "/")).name
    base = SAFE_NAME_RE.sub("_", base).strip(" .")
    if not base:
        base = default
    if "." not in base:
        base += pathlib.PurePath(default).suffix or ".png"
    stem, _, ext = base.rpartition(".")
    if ext.lower() not in MEDIA_EXT:
        return f"{stem or 'upload'}.png"
    return base


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
    try:
        base = Image.open(
            BytesIO(comfy_bytes(f"/view?filename={q(base_name)}&type=input"))
        ).convert("RGB")
        mask = Image.open(
            BytesIO(comfy_bytes(f"/view?filename={q(mask_name)}&type=input"))
        ).convert("L")
    except urllib.error.HTTPError as e:
        raise ApiError(400, f"合成遮罩失败：ComfyUI 的 input 目录里找不到原图或遮罩（HTTP {e.code}）")
    except urllib.error.URLError as e:
        raise ApiError(502, f"合成遮罩失败：连不上 ComfyUI（{e.reason}）")
    except ApiError:
        raise
    except Exception as e:
        raise ApiError(400, f"合成遮罩失败：{e}")
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
        body = r.read().decode("utf-8", "ignore").strip()
    # /interrupt 这类接口成功时返回空响应，不能直接当 JSON 解析
    return json.loads(body) if body else {}


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


def refresh_info_cache():
    """清空节点信息缓存并重新探测。

    ComfyUI 里新装了模型/LoRA、或者重启过后，下拉选项要靠这个刷新，
    否则旧列表会一直用下去（以前只能删 .cache 目录或重启服务）。
    """
    with _cache_lock:
        _info_cache.clear()
        _failed_types.clear()
        _cache.clear()
    _save_info_cache()
    _comfy_check["ok"] = None
    _comfy_check["ts"] = 0
    return {"ok": True, "comfy": comfy_available()}


def wf_path(wf_id):
    """把前端传来的 id 解析成 api_workflows 下的真实文件，杜绝目录穿越。"""
    raw = urllib.parse.unquote(str(wf_id or "")).strip()
    if not raw or raw.startswith(("/", "\\")) or ":" in raw:
        raise ApiError(404, "工作流不存在")
    base = API_DIR.resolve()
    try:
        p = (base / raw).resolve()
    except (OSError, ValueError):
        raise ApiError(404, "工作流不存在")
    if not p.is_relative_to(base) or p == base or not p.is_file():
        raise ApiError(404, "工作流不存在")
    if not p.name.endswith(".api.json"):
        raise ApiError(404, "工作流不存在")
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


def media_kind(key, spec_type, spec_meta, choices, val):
    """判断一个字段是不是「要传文件」，返回 image / video / None。

    不能只看字段名：ComfyUI 的 LoadImage 把 image 声明成 COMBO（候选是 input 目录里的
    文件名，带 image_upload 标记），而别的节点可能叫 input_image / image1。
    所以按「节点声明的上传标记 → 类型 → 候选扩展名 → 字段名」逐级兜底。
    """
    if not isinstance(val, str):
        return None
    low = key.lower()
    if spec_meta.get("video_upload"):
        return "video"
    if spec_meta.get("image_upload"):
        return "image"
    if spec_type == "IMAGE":
        return "image"
    if spec_type == "VIDEO":
        return "video"
    if spec_type == "MASK":
        return None

    if choices:
        exts = {
            str(c).rsplit(".", 1)[-1].lower()
            for c in choices
            if isinstance(c, str) and "." in str(c)
        }
        if exts:
            hit = exts & MEDIA_EXT
            # 候选里绝大多数是图片/视频扩展名，才当成上传位（.safetensors 那类不能误判）
            if hit and len(hit) * 2 >= len(exts):
                return "video" if len(hit & VIDEO_EXT) > len(hit & IMAGE_EXT) else "image"
            return None

    if spec_type is None or spec_type == "COMBO":
        if low in IMAGE_KEYS or re.fullmatch(r"(image|img|input_image|source_image)[ _-]?\d*", low):
            return "image"
    return None


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
            media = media_kind(key, spec_type, spec_meta, choices, val)
            if media:
                kind, val = media, ""
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
            if choices and kind not in ("image", "video"):
                item["choices"] = choices[:200]
            if kind in ("image", "video"):
                item["accept"] = "video/*" if kind == "video" else "image/*"
            if kind == "number":
                # 把节点的取值范围一起带给手机端，步进按钮才不会跳出范围
                for mk in ("min", "max", "step"):
                    mv = spec_meta.get(mk)
                    if isinstance(mv, (int, float)) and not isinstance(mv, bool):
                        item[mk] = mv
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
            elif kind in ("image", "video"):
                item["label"] = f"{title} · {'视频' if kind == 'video' else '图片'}"
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
        word = "参考视频" if p["kind"] == "video" else "参考图"
        p["label"] = f"{word} {i + 1}" if len(imgs) > 1 else word
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
    wf_id = p.name  # 统一成磁盘上的真实文件名，不回传 URL 编码后的 id
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
    # 界面配置变了，旧的详情缓存（按 mtime 做 key）就没意义了，顺手清掉防止越攒越多
    with _cache_lock:
        for k in [k for k in _cache if k[0] == str(p)]:
            _cache.pop(k, None)
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
                "group": it.get("group") or "",
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
        k = f.get("key")
        if not k:
            raise ApiError(400, "参数缺少 key 字段，可能是不完整的前端请求")
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


SEED_KEYS = ("seed", "noise_seed", "rand_seed", "seed_value", "int_seed")


def build_prompt(wf_id, values):
    prompt = copy.deepcopy(workflow_detail(wf_id)["prompt"])
    values = values or {}

    # 种子自动随机：界面上没暴露的 seed 字段每次运行都换新值，避免重复出同一张图
    for nid, node in prompt.items():
        inputs = node.get("inputs") or {}
        if not any(k in inputs for k in SEED_KEYS):
            continue
        info = object_info(node.get("class_type", ""))
        req = (info.get("input") or {}).get("required") or {}
        opt = (info.get("input") or {}).get("optional") or {}
        for key in SEED_KEYS:
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
    filename = safe_filename(filename)
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
        if VERBOSE:
            sys.stderr.write("[%s] %s - %s\n" % (time.strftime("%H:%M:%S"), self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra or ():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def _page(self, filename):
        f = WEB_DIR / filename
        if not f.exists():
            return self._err(500, f"缺少页面文件：{filename}")
        extra = ()
        if TOKEN:
            extra = (("Set-Cookie", f"cctk={TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"),)
        return self._send(200, f.read_bytes(), "text/html; charset=utf-8", extra)

    def _query_token(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (q.get("t", [""])[0] or "").strip()

    def _authorized(self):
        """本机回环直接放行；其他设备要求 ?t= / X-Token / 之前发过的 Cookie。"""
        if not TOKEN:
            return True
        try:
            if ipaddress.ip_address(self.client_address[0].replace("::ffff:", "")).is_loopback:
                return True
        except ValueError:
            pass
        for got in (
            self.headers.get("X-Token") or "",
            self._query_token(),
        ):
            got = got.strip()
            if got and secrets.compare_digest(got, TOKEN):
                return True
        m = re.search(r"(?:^|;\s*)cctk=([^;]+)", self.headers.get("Cookie") or "")
        if m:
            got = urllib.parse.unquote(m.group(1)).strip()
            if got and secrets.compare_digest(got, TOKEN):
                return True
        return False

    def _no_auth_page(self):
        html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>需要访问令牌</title>
<style>body{{background:#0b0b0e;color:#f2f2f7;font:16px/1.7 -apple-system,"PingFang SC",sans-serif;
padding:40px 20px;max-width:520px;margin:0 auto}}code{{background:#1e1e25;padding:2px 6px;border-radius:5px;
word-break:break-all;font-size:13px}}p{{color:#8a8a95}}.y{{color:#ffb44d}}</style></head><body>
<h2>需要访问令牌</h2>
<p>这个控制台开了访问令牌保护，直接用这个地址打不开。</p>
<p>去电脑上看启动时那个黑窗口，用里面显示的<b>「局域网访问」/「外网(Tailscale)」</b>完整地址
（结尾带 <code>?t=...</code>）打开一次，之后这台设备就不用再输了。</p>
<p class="y">如果忘了地址，重启一次服务也会重新打印。</p>
</body></html>"""
        return self._send(401, html, "text/html; charset=utf-8")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                if not self._authorized():
                    return self._no_auth_page()
                return self._page("index.html")
            if u.path == "/admin":
                if not self._authorized():
                    return self._no_auth_page()
                return self._page("admin.html")
            if u.path == "/api/info":
                info = {"addresses": local_addresses(), "comfy": COMFY, "port": PORT}
                # 只有已授权的请求才回令牌，方便管理页显示「手机该打开哪个地址」
                if TOKEN and self._authorized():
                    info["token"] = TOKEN
                return self._send(200, info)
            if u.path.startswith("/api/") and not self._authorized():
                return self._err(401, "需要访问令牌，请用启动窗口里带 ?t= 的地址打开")

            if u.path.startswith("/api/admin/"):
                if not is_admin_client(self.client_address[0]):
                    return self._err(403, "管理接口只允许在本机或局域网访问")
                if u.path == "/api/admin/list":
                    return self._send(200, {"items": list_workflows()})
                if u.path == "/api/admin/detail":
                    return self._send(200, admin_detail(q.get("id", [""])[0]))
                return self._err(404, "not found")
            if u.path == "/api/workflows":
                return self._send(200, {"items": list_workflows()})
            if u.path.startswith("/api/workflow/"):
                return self._send(200, workflow_detail(u.path[len("/api/workflow/"):]))
            if u.path == "/api/job":
                pid = q.get("id", [""])[0]
                if not pid:
                    return self._err(400, "缺少任务 id")
                return self._send(200, comfy_get(f"/history/{urllib.parse.quote(pid)}"))
            if u.path == "/api/queue":
                # 手机端用它算排队位置和当前在跑的任务
                return self._send(200, comfy_get("/queue"))
            if u.path == "/api/view":
                fn = q.get("filename", [""])[0]
                if not fn:
                    return self._err(400, "缺少 filename")
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
        except ApiError as e:
            return self._err(e.code, e.msg)
        except urllib.error.URLError as e:
            return self._err(502, f"连不上 ComfyUI：{e.reason}")
        except Exception as e:
            return self._err(500, f"服务端出错：{e}")

    def _json_body(self, raw):
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if not u.path.startswith("/api/"):
                return self._err(404, "not found")
            if not self._authorized():
                self.close_connection = True
                return self._err(401, "需要访问令牌，请用启动窗口里带 ?t= 的地址打开")

            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.close_connection = True
                return self._err(400, "Content-Length 不合法")
            if n < 0 or n > MAX_UPLOAD:
                self.close_connection = True
                return self._err(413, f"请求体超过 {MAX_UPLOAD // 1024 // 1024} MB 上限")
            raw = self.rfile.read(n) if n else b""

            if u.path.startswith("/api/admin/"):
                if not is_admin_client(self.client_address[0]):
                    return self._err(403, "管理接口只允许在本机或局域网访问")
                body = self._json_body(raw)
                if u.path == "/api/admin/save":
                    wid = body.get("id")
                    if not wid:
                        return self._err(400, "缺少工作流 id")
                    return self._send(200, {"ok": True, "ui": save_ui(wid, body)})
                if u.path == "/api/admin/reorder":
                    wid = body.get("id")
                    if not wid:
                        return self._err(400, "缺少工作流 id")
                    return self._send(200, reorder_workflow(wid, body.get("dir", "up")))
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
                    wid = body.get("id")
                    if not wid:
                        return self._err(400, "缺少工作流 id")
                    return self._send(200, delete_workflow(wid))
                if u.path == "/api/admin/refresh-info":
                    return self._send(200, refresh_info_cache())
                return self._err(404, "not found")

            if u.path == "/api/run":
                body = self._json_body(raw)
                wid = body.get("id")
                if not wid:
                    return self._err(400, "缺少工作流 id")
                values = body.get("values") or {}
                if not isinstance(values, dict):
                    return self._err(400, "values 必须是对象")
                for v in values.values():
                    if isinstance(v, str) and v.startswith("upload:"):
                        return self._err(400, "图片还没上传成功")
                prompt = build_prompt(wid, values)
                res = comfy_post("/prompt", {"prompt": prompt, "client_id": uuid.uuid4().hex})
                return self._send(200, res)
            if u.path == "/api/mask":
                body = self._json_body(raw)
                base, mask = body.get("base"), body.get("mask")
                if not base or not mask:
                    return self._err(400, "缺少 base / mask 文件名")
                return self._send(200, {"name": compose_mask(base, mask)})
            if u.path == "/api/upload":
                if not raw:
                    return self._err(400, "上传内容为空")
                fname = self.headers.get("X-Filename") or "upload.png"
                return self._send(200, upload_image(raw, fname))
            if u.path == "/api/interrupt":
                return self._send(200, comfy_post("/interrupt", {}))
            return self._err(404, "not found")
        except ApiError as e:
            return self._err(e.code, e.msg)
        except ValueError as e:
            return self._err(400, str(e))
        except FileNotFoundError:
            return self._err(404, "工作流不存在")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:600]
            return self._err(400, f"ComfyUI 拒绝执行：{detail}")
        except urllib.error.URLError as e:
            return self._err(502, f"连不上 ComfyUI：{e.reason}")
        except Exception as e:
            return self._err(500, f"服务端出错：{e}")


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
    tok = f"?t={TOKEN}" if TOKEN else ""
    print("")
    print("  ComfyUI 手机控制台")
    print("  " + "-" * 46)
    print(f"  ComfyUI 后端 : {COMFY}")
    print(f"  工作流目录   : {API_DIR}")
    print(f"  本机管理页   : http://127.0.0.1:{PORT}/admin")
    for ip in addr["lan"]:
        print(f"  局域网访问   : http://{ip}:{PORT}/{tok}")
    for ip in addr["tailscale"]:
        print(f"  外网(Tailscale): http://{ip}:{PORT}/{tok}")
    if not addr["lan"] and not addr["tailscale"]:
        print("  未检测到可用网卡地址，仅本机可访问")
    print("  " + "-" * 46)
    if TOKEN:
        print("  手机第一次打开请用上面带 ?t= 的完整地址，之后会自动记住")
        print("  （不想要令牌：把 config.json 里的 token 改成 \"\"）")
    else:
        print("  [注意] 已关闭访问令牌，同一网络内任何设备都能操作本服务")
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

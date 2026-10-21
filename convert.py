"""把 ComfyUI UI 格式的工作流转成 API 格式。

原理：用无头 Edge 打开本机 ComfyUI 前端，调用它自己的 graphToPrompt()
把节点图转成后端能吃的 prompt 图。等价于你在界面里点「导出(API)」。
"""
import asyncio
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8188"
WF_DIR = pathlib.Path(
    r"D:/comfy-desktop/comfyui-installs/comfyui/ComfyUI/user/default/workflows"
)
OUT_DIR = pathlib.Path(__file__).parent / "api_workflows"

LOAD_JS = """
async (data) => {
  const app = window.app;
  if (!app || !app.graph) throw new Error("app/graph 未就绪");
  if (app.extensionManager && app.extensionManager.workflow) {
    await app.extensionManager.workflow.loadGraphData(data, true, true, true);
  } else {
    await app.loadGraphData(data, true, true, true);
  }
  const r = await app.graphToPrompt();
  return r.output;
}
"""


async def probe(page):
    info = await page.evaluate(
        """() => ({
            title: document.title,
            hasApp: !!window.app,
            hasGraph: !!(window.app && window.app.graph),
            hasGraphToPrompt: !!(window.app && typeof window.app.graphToPrompt === 'function'),
            hasExtManager: !!(window.app && window.app.extensionManager),
        })"""
    )
    print("探测结果:", json.dumps(info, ensure_ascii=False))
    return info


async def convert_one(page, path: pathlib.Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return await page.evaluate(LOAD_JS, data)


async def main():
    only_probe = "--probe" in sys.argv
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page()
        await page.goto(BASE, wait_until="domcontentloaded")
        await page.wait_for_function(
            "() => window.app && window.app.graph", timeout=90000
        )
        info = await probe(page)
        if only_probe or not info.get("hasGraphToPrompt"):
            await browser.close()
            return

        files = sorted(WF_DIR.rglob("*.json"))
        print(f"共发现 {len(files)} 个 workflow 文件")
        ok, fail = 0, []
        for f in files:
            rel = f.relative_to(WF_DIR)
            try:
                prompt = await convert_one(page, f)
                if not prompt:
                    raise RuntimeError("转换结果为空")
                dest = OUT_DIR / rel.with_suffix(".api.json")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(
                    json.dumps(prompt, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                ok += 1
                print(f"  [OK] {rel}  ({len(prompt)} 个节点)")
            except Exception as e:
                fail.append((str(rel), str(e).split("\n")[0][:160]))
                print(f"  [跳过] {rel} -> {str(e).split(chr(10))[0][:110]}")

        print(f"\n完成:成功 {ok},失败 {len(fail)}")
        if fail:
            (OUT_DIR / "_failed.txt").write_text(
                "\n".join(f"{n}\t{why}" for n, why in fail), encoding="utf-8"
            )
        await browser.close()


if __name__ == "__main__":
    from playwright.async_api import async_playwright

    asyncio.run(main())

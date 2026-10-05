# -*- coding: utf-8 -*-
"""upload_via_api.py —— 只走 api.github.com 发布仓库（适合 github.com 网页/推送不通的网络）。

用法：
    python upload_via_api.py <你的GitHub用户名> <仓库名> [--public]

Token 从**本地文件**读取（不会出现在命令行/聊天记录里）：
    把 token 写进仓库根目录的 .gh_token（一行，什么别的都别写），
    或者设环境变量 GH_TOKEN。
⚠️ .gh_token 已在 .gitignore 里，不会被提交。

行为：
    1) 建仓库（已存在就跳过）—— 默认**私有**（--public 改公开）；
    2) 把 `git ls-files` 列出的每个文件用 Contents API 上传（base64）；
    3) 顺带把 vauto.bundle（若存在）也传上去 —— 别人可以 clone 出完整历史。
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = "https://api.github.com"


def token() -> str:
    for p in (ROOT / ".gh_token", Path.home() / ".gh_token"):
        if p.is_file():
            t = p.read_text(encoding="utf-8").strip()
            if t:
                return t
    import os
    t = os.environ.get("GH_TOKEN", "").strip()
    if t:
        return t
    sys.exit("[!] 找不到 token：把一行 token 写进 .gh_token，或设环境变量 GH_TOKEN")


def call(method: str, url: str, tok: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "vauto-publish",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}


def main() -> int:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    user, repo = sys.argv[1], sys.argv[2]
    public = "--public" in sys.argv
    tok = token()

    # 1) 建仓库（已存在 → 422）
    st, js = call("POST", f"{API}/user/repos", tok, {
        "name": repo,
        "private": (not public),
        "description": "基于挑战蓝图的地平线六刷技能点软件 —— 《极限竞速：地平线6》CV+输入仿真（学习用途）",
        "has_issues": True, "has_wiki": False,
    })
    print(f"[仓库] {st} " + ("已创建 ✓" if st == 201 else
                             "已存在，跳过 ✓" if st == 422 else f"失败: {js}"))
    if st not in (201, 422):
        return 1

    # 2) 上传跟踪文件
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                           text=True).stdout.split("\n")
    files = [f for f in files if f.strip()]
    bundle = ROOT / "vauto.bundle"
    if bundle.is_file():
        files.append("vauto.bundle")
    ok = fail = 0
    for i, rel in enumerate(files, 1):
        p = ROOT / rel
        if not p.is_file():
            print(f"  [{i}/{len(files)}] 跳过（不在磁盘上）: {rel}")
            continue
        raw = p.read_bytes()
        st, js = call("PUT", f"{API}/repos/{user}/{repo}/contents/{rel.replace(chr(92), '/')}",
                      tok, {"message": f"upload {rel}", "content": base64.b64encode(raw).decode()})
        if st in (201, 200):
            ok += 1
            if i % 10 == 0 or i == len(files):
                print(f"  [{i}/{len(files)}] 已上传 {ok} 个…")
        else:
            fail += 1
            print(f"  [!] {rel}: {st} {js.get('message', js)}")
    print(f"\n✓ 完成：{ok} 个文件上传，{fail} 个失败")
    print(f"  仓库地址：https://github.com/{user}/{repo}")
    print(f"  （私有仓库别人看不到；要公开：网页 Settings → Change visibility）")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

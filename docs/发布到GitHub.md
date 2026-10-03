# 发布到 GitHub —— 三条命令（含网络不通的兜底）

> 背景实测（2026-10-03）：本机 `api.github.com` 通 ✓，但 `github.com`（网页/Release/直接 git push 走的域名）
> 时通时断 ✗。下面给了三种走法，按你的网络情况选。

---

## 前置（一次性）：建一个 Personal Access Token

1. 浏览器打开 https://github.com/settings/tokens （需要能访问网页 ✗ 就得挂梯子）
2. **Fine-grained token** → Repository access 选 **All repositories**（或稍后只选这个新仓库）
3. Permissions → **Repository permissions** → 以下都设 **Read and write**：
   - `Contents`（必选）
   - `Administration`（建仓库用；只推代码不需要）
4. 生成后**只显示一次**，复制下来。

⚠️ Token 等同于密码：不要提交进 git、不要贴在公开聊天里。用完可以随时在网页上撤销。

---

## 走法 A：网页建空仓库 + 直接 push（网络能访问 github.com 时最省事）

> ⚡ 本机实测（2026-10-03）：**直连 github.com 不通 ✗，但开着 Clash Verge 的代理就通 ✓**
> （代理口 `127.0.0.1:7897`，mixed 端口）。所以下面命令里加了 `-c http.proxy=…`，
> Clash 在托盘里开着即可；不通时把这一项去掉再直连试试。

```bash
cd <你的仓库目录>
# 1) 网上建一个空仓库（不要勾 README），比如叫 vauto
# 2) 关联并推（带代理）
git remote add origin https://github.com/<你的用户名>/vauto.git
git -c http.proxy=http://127.0.0.1:7897 push -u origin master
# 3) 弹窗登录（Git Credential Manager）或按提示输入用户名 + Token 当密码
```

> 若 push 报 credentials 相关错误，可只对这一次用 token：
> `git -c http.proxy=http://127.0.0.1:7897 push https://<用户名>:<Token>@github.com/<用户名>/vauto.git master`
> 用完记得去网页把该 Token 撤销（或换一个新的），避免留在别处。

推完后：仓库页 → Settings → 最下面 「Change visibility」可随时改公开/私有。

## 走法 B：只能通 api.github.com（用 API 建仓库 + 上传）

> 适合 github.com 页面/推送不通、但 api 通的环境。上传是逐文件调用，会慢一点。

```bash
cd <你的仓库目录>
TOKEN=<你的Token>          # 只在本次终端会话里用，别写进文件
USER=<你的GitHub用户名>

# 1) 建仓库（私有："private":true；公开改 false）
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Accept: application/vnd.github+json" \
     https://api.github.com/user/repos \
     -d "{\"name\":\"vauto\",\"private\":true,\"description\":\"《地平线6》视觉自动化工具（学习用途）\"}"

# 2) 上传全部跟踪文件（脚本逐文件 PUT，自动跳过二进制大小限制外的文件）
python upload_via_api.py $USER vauto
```

## 走法 C：离线包（vauto.bundle）—— 任何地方都能还原

仓库里已经有 `git bundle create vauto.bundle --all` 生成的**单文件全量备份**（含全部提交历史）。

```bash
# 在能上网的任意机器上：
git clone vauto.bundle vauto        # 得到一个完整仓库（含历史）
cd vauto
git remote set-url origin https://github.com/<你的用户名>/vauto.git
git push -u origin master
```

或者把 `vauto.bundle` 直接上传到 GitHub 的 Release / 仓库文件里当发布物。

---

## 发布前检查清单

- [x] 仓库里**没有** `logs/`（调试截图含游戏 ID、账号信息）—— 已确认 `git ls-files | grep "^logs/"` 为空；
- [x] 没有 token / 密码 / 本机绝对路径之外的敏感信息；
- [x] README 顶部有**免责声明**（EULA / 风控 / 账号风险）；
- [ ] 决定仓库**可见性**（建议先私有，确认无误再转公开）；
- [ ] （可选）打 tag 发布 exe 压缩包：`git tag v0.1 && git push origin v0.1`。

# 太墟官方插件库

太墟 PROCESS / WEB 插件分发仓库，兼容已有 v1 PROCESS 包和 v2 PROCESS/WEB 包。Code 保存签名目录、文档和脚本，软件包放 Releases，大包不提交到 Git。每个插件独立 tag，例如 `github-v1.0.2`，不使用仓库级 latest 判断插件版本。

GitHub：`taixu-lab/taixu-library`；Gitee：`wang_ke_bin/taixu-library`。两个渠道均仅发布不超过 **96 MiB** 的小插件；Android 全栈开发环境等大包保留在本地，不上传、不通过切片绕过限制。

两个渠道分别读取 `catalog.json`、`catalog-gitee.json`，各有 `.sig` 签名和独立递增 revision，使用相同 APK 固定公钥。目录只登记该渠道已经上传并校验的附件；GitHub 尚未发布的包不会因为已经发布到 Gitee 就被标为 GitHub 可下载。

## 发布

需要 Python 3.10+、`pip install -r requirements.txt`，发布 GitHub 需已登录的 GitHub CLI。先用太墟 `taixu-plugin check/pack/inspect` 生成 v2 包；脚本还会复核 payload 文件集合与哈希。v1 使用固定 `legacy.tool-<id>` 身份，整包签名哈希和原有本地导入检查仍生效。

上传 Gitee 需在本机进程环境中设置 `GITEE_ACCESS_TOKEN`，不要写进命令参数、配置文件或仓库。令牌只用于 Gitee API 请求头；公开附件回下载校验不携带令牌。

私钥位于 `%USERPROFILE%\.codex\keys\taixu-library-signing.pem`，请单独备份，不能提交或分享。`public-key.txt` 是公开公钥，已经同步固定到太墟 APK。更换公钥需同步发布新版 App。

```powershell
# 准备附件及下一版签名目录，不执行远程操作
python scripts/library.py prepare C:\packages\example.txplugin --private-key "$env:USERPROFILE\.codex\keys\taixu-library-signing.pem"

# 复核 dist/<toolId>-v<version> 后上传 Release
python scripts/library.py publish dist/example-v1.0.0

# 只发布 Gitee（不需要 GitHub CLI，不会登记未上传的 GitHub 地址）
python scripts/library.py publish-gitee dist/example-v1.0.0

# 将已发布到 GitHub 且本机 dist 仍有附件的小插件补同步到 Gitee
python scripts/library.py sync-gitee

# 如仅需发布 GitHub，可显式跳过小插件镜像
# python scripts/library.py publish dist/example-v1.0.0 --github-only

# 复核目录变更后提交并推送
git add catalog.json catalog.json.sig catalog-gitee.json catalog-gitee.json.sig
git commit -m "feat(library): 发布示例插件 1.0.0"
git push origin main
```

第一次启用需将本仓库的配置、脚本、公钥及签名目录提交到 main。首次创建全新信任源才使用 `keygen --private-key <仓库外路径>`，现有库不要重复生成。

当前本地 checkout 已配置 origin 双 push URL 和独立 gitee remote，同一次提交执行 `git push origin main` 会依次推送 GitHub、Gitee。新克隆可在仓库根目录运行 `python scripts/configure_remotes.py` 恢复配置。跨平台推送不具备原子性，任一端失败需检查并重试，不使用强推；Git push 只同步代码和目录，Release 附件由发布命令单独同步。

prepare 保留离线生成分片候选的能力，但 publish / publish-gitee 均拒绝超过 96 MiB 的包，准备候选不代表允许上传。客户端分片下载能力保留，当前官方目录只登记小插件。

上传成功且公开附件回下载哈希验证成功后才更新对应 Gitee 目录。已有同名附件先验证完整哈希再复用，内容不符会拒绝覆盖。总仓库容量由 Gitee 服务端执行，额度不足会明确失败，不会伪造镜像地址。请串行发布；部分附件上传后失败可重试；目录和代码的 Git 提交/推送仍由用户执行。

## 目录协议 v1

根字段：`schemaVersion: 1`、递增 `revision`、`plugins`。每个插件包含 `pluginId/toolId/name/description/execution/releases`。每版记录稳定 SemVer `version`、`channel: stable`、`platform: linux-arm64`、`packageSchema`（缺省 2）、`pluginApi.minimum/maximum`、整包字节 `size/sha256` 和有序 `parts`。每片记录 HTTPS `url`、`size`、小写 SHA-256；可选 `mirrors.GITEE` 是已上传验证的镜像地址，单文件也用一项 parts。目录签名为原始文件字节的 Ed25519 签名，再 Base64 编码。

目录最多 1 MiB，单包最多 8 GiB、64 片。客户端选 API 兼容的最高稳定版本。PROCESS 与当前本地发行版实际安装版本比较，WEB 与当前导入版本比较。下载后仍执行原生安装确认和能力授权，目录不授予权限。

包内保留第三方组件许可证、来源和再分发说明。流量增长后可将 parts 的 HTTPS 地址改为外部存储。

## 测试

`python -m unittest discover -s tests -v`

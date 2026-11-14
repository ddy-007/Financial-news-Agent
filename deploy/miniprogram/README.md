# 小程序只读联调网关

此配置供具名测试人员通过 HTTPS 核对接口。默认只监听 `127.0.0.1:8443`，上游 FastAPI 也必须只监听回环地址。网关使用单个测试账号，账号和密码哈希由运行环境提供。小程序仍可使用原有本地联调方式。此网关通过命令行验收，现有小程序还没有接入网关登录。 [Caddy HTTPS 文档](https://caddyserver.com/docs/automatic-https) · [Caddy 鉴权文档](https://caddyserver.com/docs/caddyfile/directives/basic_auth)

| 允许的请求 | 网关限制 |
| --- | --- |
| `GET /api/v1/reports/today` | 最新日报 |
| `GET /api/v1/reports` | 固定为最近 10 份日报 |
| `GET /api/v1/news` | 保留关键词，每次最多 20 条，移除分页和日期范围参数 |
| `GET /api/v1/news/{news_id}/timeline` | 仅匹配 UUID 形式的新闻 ID，保留 `as_of` |

这四个接口凭据缺失或错误时返回 `401`，凭据不会传给 FastAPI。默认配置下，AI 问答、采集、报告生成、诊断和其他路径或方法均返回 `404`。网关关闭访问日志和配置快照，从错误日志中移除请求字段。部署前还需检查上游和其他代理的日志是否记录搜索词。 [Caddy 请求匹配文档](https://caddyserver.com/docs/caddyfile/matchers) · [Caddy 全局配置](https://caddyserver.com/docs/caddyfile/options)

## 离线验收

在项目根目录运行以下命令。验收环境为 Python 3.11 和 Caddy 2.11.9。脚本会启动回环地址上的临时 HTTPS 网关和模拟上游，在独立临时目录生成证书并验证 TLS，不修改系统信任库。

```powershell
.\.venv\Scripts\python.exe -B scripts/check_miniprogram_gateway.py --caddy caddy
```

## 配置与启动

在项目根目录打开交互式 PowerShell。`hash-password` 会提示输入测试密码。下一行只粘贴命令输出的哈希。启动时使用当前进程环境，实际部署应由服务管理器注入并保护环境变量。 [Caddy CLI 文档](https://caddyserver.com/docs/command-line#caddy-hash-password)

```powershell
$env:MINIPROGRAM_GATEWAY_USER = 'tester'
caddy hash-password --algorithm bcrypt
$env:MINIPROGRAM_GATEWAY_HASH = Read-Host '粘贴上一步输出的密码哈希'
caddy validate --config deploy/miniprogram/Caddyfile --adapter caddyfile
if ($LASTEXITCODE -eq 0) { caddy run --config deploy/miniprogram/Caddyfile --adapter caddyfile }
```

默认上游端口是 `8000`，可用 `MINIPROGRAM_UPSTREAM_PORT` 修改。保持 FastAPI 监听 `127.0.0.1`。默认 Caddy 数据目录下的 `pki/authorities/local/root.crt` 可供测试客户端单次信任。另开 PowerShell 执行以下命令，按提示输入测试密码。 [Caddy 数据目录](https://caddyserver.com/docs/conventions#data-directory)

```powershell
curl.exe --noproxy 127.0.0.1 --ssl-revoke-best-effort --cacert "$env:APPDATA\Caddy\pki\authorities\local\root.crt" --user tester https://127.0.0.1:8443/api/v1/reports/today
```

Windows 自带的 Schannel 版 curl 需要 `--ssl-revoke-best-effort` 处理本地 CA 缺少吊销信息的情况，证书链和主机名仍会校验。使用 `Ctrl+C` 即可停掉前台网关，原有 FastAPI 独立运行。

## 部署探测

在运行 FastAPI 和 Caddy 的主机上执行。探针分别通过回环地址读取 FastAPI 的就绪状态、通过带认证的 HTTPS 读取最新日报。使用本机 Caddy 证书时，在项目根目录打开 PowerShell 并运行以下命令。

```powershell
$env:MINIPROGRAM_GATEWAY_USER = 'tester'
.\.venv\Scripts\python.exe -B scripts/probe_miniprogram.py --prompt-password --ca-file "$env:APPDATA\Caddy\pki\authorities\local\root.crt"
```

正式 HTTPS 网关可用 `--gateway https://你的网关域名` 指定，使用受信任证书时省略 `--ca-file`。若上游监听其他端口，用 `--upstream-port` 指定。接入部署平台时，使用受保护的 `MINIPROGRAM_PROBE_PASSWORD` 环境变量并省略 `--prompt-password`。参数和本地 CA 文件通过校验后，探针输出一行 JSON，只记录状态、HTTP 状态码、耗时、已知依赖状态及日报业务日。参数或 CA 文件错误会写到标准错误流。

| 退出码 | 含义 |
| --- | --- |
| `0` | 后端就绪，网关正常返回日报 |
| `1` | 后端报告降级，或后端明确返回暂无日报 |
| `2` | 后端不可用、认证或 TLS 失败、接口响应异常，或探针参数错误 |

探针展示最近一期日报的业务日，周末没有新日报不会因此告警。暂停调度时，若后端就绪接口报降级，可通过 `scheduler_running` 判断原因。探针不会生成日报、恢复调度或调用模型。

## 本机问答内测

`MINIPROGRAM_CHAT_ENABLED=1` 让 Caddy 将来自回环地址的 `POST /api/v1/agent/chat` 转发给独立接入层。内测时让 Caddy、接入层和原 FastAPI 均监听回环地址，并从同一台主机直接连接 Caddy。内测通过独立分发的 Bearer 凭据识别具名测试者。额度只约束网关路径，能在本机访问原 FastAPI 的用户仍能直接调用原接口，内测应在受信任的主机上进行。现有小程序仍使用原联调方式。采集、报告生成、诊断和内部路径始终返回 `404`。

1. 为每人生成独立的高熵凭据，例如通过 Python `secrets.token_urlsafe(32)`。将原始凭据单独交给对应测试者。把测试者代号及其凭据的 SHA-256 小写十六进制摘要注入接入层的 `MINIPROGRAM_CHAT_USERS` 环境变量。格式为 JSON 对象，键为代号，值为摘要。撤销或轮换凭据时更新摘要并重启接入层。同一人的代号应保持稳定，以保留当天用量。
2. 将 `MINIPROGRAM_CHAT_QUOTA_DB` 设为独立 SQLite 文件的绝对路径，父目录须预先存在并仅供部署账号读写。同一实例的所有接入层进程使用同一份持久文件。可通过 `MINIPROGRAM_CHAT_USER_DAILY` 和 `MINIPROGRAM_CHAT_TOTAL_DAILY` 覆盖内测默认值（每人 5 次、合计 20 次）。从仓库根目录运行接入层，先保持 Caddy 问答入口关闭。

   ```powershell
   .\.venv\Scripts\python.exe -B -m uvicorn deploy.miniprogram.chat_gate:create_app --factory --host 127.0.0.1 --port 8001 --no-access-log
   ```

3. 在 Caddy 进程环境中设置 `MINIPROGRAM_CHAT_ENABLED=1`，按前文验证配置并重启网关。上游 FastAPI 使用 `MINIPROGRAM_UPSTREAM_PORT`，接入层与 Caddy 必须使用相同的上游配置。若修改接入层监听端口，同步设置 Caddy 的 `MINIPROGRAM_CHAT_PORT`。将 `MINIPROGRAM_CHAT_ENABLED` 恢复为 `0` 后重启 Caddy，即可关闭内测入口。

接入层先校验身份和输入，再扣减个人与全局的当日额度，按北京时间零点重置。超额返回 `429` 和 `Retry-After`，配额库故障返回 `503`。已放行的请求即使在上游失败也计入额度，因为可能已发生模型调用。额度按问答请求计数，单次问答可能产生多个模型调用，因此这不是供应商费用上限。当前方案仅支持单机试点。 [SQLite 事务说明](https://www.sqlite.org/lang_transaction.html)

离线验收使用真实 Caddy、临时接入层和临时配额库，上游为模拟服务。

```powershell
.\.venv\Scripts\python.exe -B scripts/check_miniprogram_chat_gate.py --caddy caddy
```

## 微信登录本地联调

在接入层进程环境中设置 `MINIPROGRAM_WECHAT_APP_ID` 与 `MINIPROGRAM_WECHAT_APP_SECRET`，然后重启接入层。仅使用微信身份联调时，可省略 `MINIPROGRAM_CHAT_USERS`。Caddy 环境同时设置 `MINIPROGRAM_CHAT_ENABLED=1` 与 `MINIPROGRAM_WECHAT_LOGIN_ENABLED=1`，校验后重启。两个入口仍限定为回环访问。 [微信登录](https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/login.html) · [登录凭证校验](https://developers.weixin.qq.com/miniprogram/dev/server/API/user-login/api_code2session.html)

在 `miniprogram/utils/chat-config.js` 填入本地 HTTPS 网关源地址，例如 `https://127.0.0.1:8443`。开发者工具需使用同一个 AppID，且信任本地 Caddy 证书。发送问题时，小程序通过 `wx.login` 获取临时凭证，由接入层向微信校验。接入层只返回有效期一小时的随机会话令牌，SQLite 仅保存令牌摘要、用户代号及到期时间。小程序将令牌保存在运行内存中。登录失败或令牌失效时，由用户重新发送问题，避免自动重发问答。

微信用户在同一个 AppID 下重新登录仍共享当天个人额度，也与内测账号共享全局额度。AppSecret 和微信会话密钥只在服务端处理。日报和新闻继续读取现有本地接口。回退时清空小程序配置中的网关地址，并关闭 `MINIPROGRAM_WECHAT_LOGIN_ENABLED`。如需立即停止现有微信会话访问，同时关闭问答入口，或从接入层环境移除微信配置并重启接入层。

接入层默认在任意连续 60 秒内最多向微信交换 12 次凭证。可用 `MINIPROGRAM_WECHAT_LOGIN_PER_MINUTE` 调整为 `1` 至 `120`，重启后生效。同一实例的进程用同一份 SQLite 计数，进程重启会保留窗口内用量。输入校验失败不计入，已放行的交换即使被微信拒绝或超时也计入。超额返回 `429` 和 `Retry-After`，计数库不可用返回 `503`，两种情况均不会向微信发请求。登录交换和问答日配额独立计算。

## 正式上线前

需要确定合法 HTTPS 域名、证书、对外监听地址及访问范围，并在真实 AppID 下验收微信登录和问答。对外开放前还需确定读接口的身份策略，并在入口增加按可信客户端来源的限流，以免单个来源占满全局交换额度。测试凭据、模型密钥和通用管理员凭据均由服务端或测试人员保管，不进入小程序包。 [微信小程序网络说明](https://developers.weixin.qq.com/miniprogram/dev/framework/ability/network.html)

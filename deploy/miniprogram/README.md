# 小程序只读联调网关

此配置供具名测试人员通过 HTTPS 核对接口。默认只监听 `127.0.0.1:8443`，上游 FastAPI 也必须只监听回环地址。默认读接口使用单个测试账号，账号和密码哈希由运行环境提供。小程序可使用原有本地联调方式，也可按下文启用微信会话。 [Caddy HTTPS 文档](https://caddyserver.com/docs/automatic-https) · [Caddy 鉴权文档](https://caddyserver.com/docs/caddyfile/directives/basic_auth)

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

历史日报列表 `GET /api/v1/reports` 支持 gzip 压缩。客户端声明支持 gzip 且响应达到压缩阈值时，Caddy 才会压缩。解压后仍是同一份 JSON，鉴权和 `Cache-Control: no-store` 继续生效。需要回退时，移除 Caddyfile 中历史日报路由的 `encode gzip` 后重新部署该网关配置。 [Caddy 压缩说明](https://caddyserver.com/docs/caddyfile/directives/encode)

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
| `2` | 后端不可用、认证或 TLS 失败、接口响应异常、边界检查失败，或探针参数错误 |

探针展示最近一期日报的业务日，周末没有新日报不会因此告警。暂停调度时，若后端就绪接口报降级，可通过 `scheduler_running` 判断原因。探针不会生成日报、恢复调度或调用模型。

从验收电脑只核对 HTTPS 网关时，使用 `--gateway-only`。发布前可加 `--verify-boundary`，额外验证四个读接口拒绝匿名请求，以及携带运维凭据访问 OpenAPI、就绪探针等内部读路径仍返回 `404`。探针只发 GET 请求，使用标准证书和主机名校验，报告域名解析、TLS 校验或不可达等错误。验收电脑须在网关允许范围内，手机实际网络和微信合法域名仍需从微信验收。

```powershell
.\.venv\Scripts\python.exe -B scripts/probe_miniprogram.py --gateway https://你的网关域名 --gateway-only --verify-boundary --prompt-password
```

微信接入层通过进程日志输出 `[miniprogram_gate]` 记录，包含操作名、接入层 HTTP 状态、结果类别、处理总耗时及服务端生成的请求编号。联调时可从 Network 面板的 `X-Miniprogram-Request-ID` 响应头找到对应记录。对读取请求，这条记录仅覆盖会话校验，成功时记录 `204`，最终数据请求的状态和耗时以 Network 面板为准。问答上游返回有效的 `X-Request-ID` 时，接入层会在响应中透传该编号，并记录为 `upstream_request_id`，用于查 Agent 日志。接入层在收到上游响应前超时时，可先查接入层自己的请求编号。网关在接入层之前拒绝的请求不会有接入层记录。

遇到 `429` 时，`local_login_limit` 表示本地登录交换额度不足，`wechat_login_limit` 表示微信返回限流，`chat_quota_reached` 表示问答日配额用完。接入层日志不记录问题正文、微信凭证、请求来源地址或会话令牌。启动命令仍使用 `--no-access-log`。日志格式随接入层和 Caddy 配置一起部署或回退。

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

在接入层进程环境中设置 `MINIPROGRAM_WECHAT_APP_ID` 与 `MINIPROGRAM_WECHAT_APP_SECRET`，然后重启接入层。仅使用微信身份联调时，可省略 `MINIPROGRAM_CHAT_USERS`。Caddy 环境同时设置 `MINIPROGRAM_CHAT_ENABLED=1` 与 `MINIPROGRAM_WECHAT_LOGIN_ENABLED=1`，校验后重启。两个入口默认限定为回环访问。 [微信登录](https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/login.html) · [登录凭证校验](https://developers.weixin.qq.com/miniprogram/dev/server/API/user-login/api_code2session.html)

在 `miniprogram/utils/chat-config.js` 填入本地 HTTPS 网关源地址，例如 `https://127.0.0.1:8443`。开发者工具需使用同一个 AppID，且信任本地 Caddy 证书。发送问题时，小程序通过 `wx.login` 获取临时凭证，由接入层向微信校验。接入层只返回有效期一小时的随机会话令牌，SQLite 仅保存令牌摘要、用户代号及到期时间。小程序将令牌保存在运行内存中。登录失败或令牌失效时，由用户重新发送问题，避免自动重发问答。

微信用户在同一个 AppID 下重新登录仍共享当天个人额度，也与内测账号共享全局额度。AppSecret 和微信会话密钥只在服务端处理。回退时清空小程序配置中的网关地址，关闭 `wechatReadEnabled` 和服务端 `MINIPROGRAM_WECHAT_LOGIN_ENABLED`。如需立即停止现有微信会话访问，同时关闭问答和微信读接口开关，或从接入层环境移除微信配置并重启接入层。

接入层默认在任意连续 60 秒内最多向微信交换 12 次凭证。可用 `MINIPROGRAM_WECHAT_LOGIN_PER_MINUTE` 调整为 `1` 至 `120`，重启后生效。同一实例的进程用同一份 SQLite 计数，进程重启会保留窗口内用量。输入校验失败不计入，已放行的交换即使被微信拒绝或超时也计入。超额返回 `429` 和 `Retry-After`，计数库不可用返回 `503`，两种情况均不会向微信发请求。登录交换和问答日配额独立计算。

单一连接来源默认每 60 秒最多交换 4 次，且受全局上限约束。使用 `MINIPROGRAM_WECHAT_LOGIN_SOURCE_PER_MINUTE` 覆盖，取值为 `1` 至全局上限。Caddy 用直接连接的地址覆盖专用来源头，接入层只需保存地址摘要。IPv4 按地址计算，非回环 IPv6 按 `/64` 计算；同一个出口后的用户可能共享额度。接入层须由本机受信任的 Caddy 调用。若 Caddy 前还有代理，当前配置会将该代理视为一个来源，应按实际代理拓扑重新验证来源边界。 [Caddy 连接地址占位符](https://caddyserver.com/docs/caddyfile/concepts#placeholders)

如需回退版本，先关闭 Caddy 微信登录入口，再同时回退 Caddy 和接入层。原全局计数表可继续使用，无需重建 SQLite 文件。

### 日报和新闻共用微信会话

完成问答登录联调后，可在 Caddy 环境增加 `MINIPROGRAM_WECHAT_READ_ENABLED=1` 并重启，同时把 `miniprogram/utils/chat-config.js` 的 `wechatReadEnabled` 设为 `true`。最新日报、历史日报、关键词新闻和事件时间线会走同一个 HTTPS 网关。首次读取需要微信登录，已有会话会直接复用。运维探针仍可在原地址使用 Basic Auth。微信读接口默认只接受本机回环请求，过期令牌返回 `401`，会话库故障时会拒绝读取。读取不消耗问答额度，请求发往原 FastAPI 前会移除凭据。

开发版本地联调可单独回退读接口。关闭客户端 `wechatReadEnabled`，在 Caddy 环境移除 `MINIPROGRAM_WECHAT_READ_ENABLED` 或设为 `0` 并重启，问答联调可继续使用。体验版和正式版需要保持读接口鉴权启用，关闭客户端开关会同时阻止读取和问答。

## 指定设备真机验收

确认测试手机能直连 Caddy，且手机信任网关使用的 HTTPS 证书。让 FastAPI 和微信接入层继续监听 `127.0.0.1`。将 `MINIPROGRAM_GATEWAY_HOST` 设为证书匹配的网关主机名和端口，将 `MINIPROGRAM_GATEWAY_BIND` 设为网关主机可达的单个网卡地址。仅把测试手机的来源地址追加到 `MINIPROGRAM_TEST_CLIENT_RANGES`，多个 IP 或 CIDR 用空格分隔。网关按直接连接来源匹配，反代或 NAT 场景须先确认它实际看到的地址。 [Caddy 来源匹配](https://caddyserver.com/docs/caddyfile/matchers#remote_ip)

1. 按本节前文启用微信登录、读取与问答，校验 Caddy 配置后启动。在小程序中填写相同的 HTTPS 网关地址，使用与接入层一致的 AppID。非调试验收应使用微信已配置的合法请求域名和受信任证书。
2. 在允许的手机上打开小程序，确认能登录、读取日报和历史列表。先用模拟上游验证发送与来源，真实问答按授权验证一次，并记录状态码、耗时及页面是否可继续操作。
3. 结束验收后从 Caddy 环境移除 `MINIPROGRAM_TEST_CLIENT_RANGES`，恢复回环监听并重启。配置路径和证书由实际部署环境提供。

登录和问答受来源白名单限制。启用微信读取时，四个读接口也受同一限制，包含 Basic Auth 请求。若运维探针继续从本机运行，`--gateway` 应指向本机能访问且证书匹配的网关地址。

## 正式上线前

需要确定合法 HTTPS 域名、证书、对外监听地址及访问范围，并在真实 AppID 下验收微信登录、读取和问答。按实际入口拓扑核对限流对应的连接来源。测试凭据、模型密钥和通用管理员凭据均由服务端或测试人员保管，不进入小程序包。 [微信小程序网络说明](https://developers.weixin.qq.com/miniprogram/dev/framework/ability/network.html)

体验版和正式版应在 `miniprogram/utils/chat-config.js` 配置已登记的 HTTPS 网关域名，并设 `wechatReadEnabled: true`。配置缺失或仍指向本机 IP、`localhost` 时，小程序会在发请求前显示配置错误。域名是否登记、证书是否可信仍由微信运行环境验证。开发版可继续用原有回环地址，联调识别不到小程序版本时也会提示配置错误。 [微信版本环境接口](https://developers.weixin.qq.com/miniprogram/dev/api/open-api/account-info/wx.getAccountInfoSync.html)

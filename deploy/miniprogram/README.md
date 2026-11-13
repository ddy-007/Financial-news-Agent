# 小程序只读联调网关

此配置供具名测试人员通过 HTTPS 核对接口。默认只监听 `127.0.0.1:8443`，上游 FastAPI 也必须只监听回环地址。网关使用单个测试账号，账号和密码哈希由运行环境提供。小程序仍可使用原有本地联调方式。此网关通过命令行验收，现有小程序还没有接入网关登录。 [Caddy HTTPS 文档](https://caddyserver.com/docs/automatic-https) · [Caddy 鉴权文档](https://caddyserver.com/docs/caddyfile/directives/basic_auth)

| 允许的请求 | 网关限制 |
| --- | --- |
| `GET /api/v1/reports/today` | 最新日报 |
| `GET /api/v1/reports` | 固定为最近 10 份日报 |
| `GET /api/v1/news` | 保留关键词，每次最多 20 条，移除分页和日期范围参数 |
| `GET /api/v1/news/{news_id}/timeline` | 仅匹配 UUID 形式的新闻 ID，保留 `as_of` |

这四个接口凭据缺失或错误时返回 `401`，凭据不会传给 FastAPI。AI 问答、采集、报告生成、诊断和其他路径或方法均返回 `404`。网关关闭访问日志和配置快照，从错误日志中移除请求字段。部署前还需检查上游和其他代理的日志是否记录搜索词。 [Caddy 请求匹配文档](https://caddyserver.com/docs/caddyfile/matchers) · [Caddy 全局配置](https://caddyserver.com/docs/caddyfile/options)

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

## 正式上线前

需要确定合法 HTTPS 域名、证书、对外监听地址及访问范围，并在真实 AppID 下验收。用户身份验证与服务端逐用户用量限制接入后，再评审开放问答接口。测试账号密码、模型密钥及通用管理员凭据均由服务端或测试人员保管，不进入小程序包。 [微信小程序网络说明](https://developers.weixin.qq.com/miniprogram/dev/framework/ability/network.html)

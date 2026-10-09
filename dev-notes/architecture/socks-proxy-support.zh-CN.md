# SOCKS 代理支持

Tau 使用 `httpx` 发送提供商请求、刷新 OAuth token 和执行启动更新检查。`httpx` 会读取 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 和 `NO_PROXY` 等标准代理环境变量。

## 变更内容

Issue #221 报告了当环境中包含以下通用 SOCKS 代理 URL 时的故障：

```bash
ALL_PROXY=socks://127.0.0.1:1080
```

`httpx` 不接受通用的 `socks://` scheme。它接受 `socks5://` 和 `socks5h://` 等明确的 SOCKS scheme，而这些 scheme 需要可选的 SOCKS 依赖。

Tau 现在：

- 在基础包中安装 `httpx[socks]`，确保 `socksio` 可用；
- 在构造 Tau 所有的 HTTP 客户端之前，将 `socks://...` 标准化为 `socks5://...`；
- 通过 `tau_ai.http` 中的共享辅助函数路由提供商客户端、OAuth token 刷新客户端和更新检查获取请求。

## 为什么将 `socks://` 映射到 `socks5://`

通用 scheme 未指定 DNS 查询应在本地还是通过代理执行。Tau 将其视为使用本地 DNS 解析的 SOCKS5，因为这是最接近的明确 `httpx` scheme，并且除了让先前无效的 URL 可用之外，不会静默改变 DNS 行为。

需要代理端 DNS 解析的用户应显式设置 `socks5h://` URL。

## 未来改进：避免临时修改环境

当前辅助函数会在构造 Tau 所有的 `httpx` 客户端时临时标准化代理环境变量。对于同步更新检查辅助函数，标准化目前会包裹整个 `httpx.get(...)` 调用，因为 `httpx.get` 会在内部构造并使用一个短生命周期客户端。

对于当前低并发的启动更新检查路径，这种做法可以接受，但环境变量属于进程全局状态。如果 Tau 之后围绕此辅助函数执行更多并发网络操作，另一个线程或任务可能会在请求进行期间观察到标准化后的代理值。

如果这成为问题，应优先避免在执行请求时修改进程环境：

1. 将代理值标准化为本地数据；
2. 使用等效代理配置构造显式的 `httpx.Client` 或 `httpx.AsyncClient`；
3. 通过该客户端执行请求，期间不更改 `os.environ`。

实现时应保留 `NO_PROXY` 语义。`httpx` 当前在内部处理环境代理发现和无代理匹配，因此若改用显式挂载/代理配置，应包括以下测试：

- `ALL_PROXY=socks://...` 标准化；
- `HTTP_PROXY` 和 `HTTPS_PROXY` 处理；
- 小写代理环境变量；
- `NO_PROXY=*` 绕过；
- 主机/域/IP `NO_PROXY` 条目；
- 显式 `socks5://` 和 `socks5h://` 值保持不变。

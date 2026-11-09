# 签名与跨电脑运行

## 当前限制

旧版 1.0.7 安装包使用自签名。作者电脑能验证该证书，不代表另一台电脑信任它。Windows 11 智能应用控制可直接阻止未知发布者；这与可提供“仍要运行”按钮的 SmartScreen 提示不同。

修改证书名称、再生成自签名或提供 ZIP 均不能建立公开信任。本项目不要求用户关闭智能应用控制或导入开发根证书。正式签名也不保证所有设备、企业策略和防病毒产品放行。

## 取得签名资格

- 可向受信任 CA 购买代码签名服务，确认其签署的 Windows Authenticode 证书和私钥使用方式。
- 开源项目可申请 [SignPath Foundation](https://signpath.org/)；须通过资格审核，申请不等于已开通。
- 也可评估 Microsoft 的托管签名服务，但需满足区域和身份验证要求。

目前自动化接入的是可用 PFX 私钥的方式。硬件令牌、云端 HSM、SignPath 等服务需要其专用签名步骤，不能假设新购买的证书都能导出 PFX。请先确认服务支持的 CI 方式，再采购。

## 当前 PFX 发布流程

在 GitHub 仓库 Actions secrets 配置 `CODESIGN_PFX_BASE64` 和 `CODESIGN_PFX_PASSWORD`，不要将私钥或密码提交到仓库、聊天、日志。PFX 只在 runner 临时目录写入，构建结束或失败后清理；密码通过环境变量传给子进程，不拼入命令行参数。

Actions → build-release → 输入 x.y.z 版本号 → 勾选 publish。无证书时发布会提前失败，不回退自签名。不勾选 publish 可生成开发测试 artifact；无证书的 artifact 是未签名测试包，不应当作已解决拦截的正式安装包。

本地正式构建可安全设置 `GUET_CODESIGN_PASSWORD` 环境变量，然后运行：

```powershell
powershell -File packaging\build.ps1 -Variant both -Version 1.0.8 -Release -Sign -SignMode pfx -PfxPath C:\certs\codesign.pfx
```

先签主程序，再生成并签安装包；正式签名必须返回 Valid 且有时间戳，拒绝自签名、无私钥和不在有效期内的证书。Release 模式要求 Inno Setup，禁止缺少浏览器的 full 包。开发自签名仍可显式使用 `-Sign -SignMode selfsigned`，但不能配合 `-Release`。

## 发布前验收

1. 在未导入开发证书的干净 Windows 11 上，检查下载后的安装包和安装后主程序的 Authenticode 状态、实际发布者和时间戳。
2. 保持智能应用控制开启，实际测试安装、启动、登录及浏览器依赖。构建机签名 Valid 不能代替目标电脑实测。
3. 验证 full 内置浏览器和 lite 系统 Edge 两条路径；如系统阻止内置 DLL/PYD/浏览器组件，定位具体文件并确认其来源与签名，不要只验证外层安装包。
4. 若仍被误判，向相应安全产品提交软件审查，不承诺通过签名自动消除全部提示。

改进发布流程不会追溯改变旧版下载文件。取得正式签名后，必须重新构建、验证并发布新版本，用户重新下载才会获得新的签名。

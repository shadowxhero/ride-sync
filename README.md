# ride-sync

码表骑行轨迹多平台同步，支持 IGPSPORT 迹驰 / 行者 / Magene 迈金 顽鹿。

## 运行环境

`python3`

## 使用方法

### 本地运行

1. 克隆项目到本地
2. 执行 `pip install -r requirements.txt` 安装依赖
3. 复制 `.env.example` 为 `.env` 并配置好导入平台、导出平台以及对应平台的账号密码

- 导出平台 `EXPORT_PLATFORM` 支持 `igpsport`, `onelap`
- 导入平台 `IMPORT_PLATFORM` 支持 `imxingzhe`

4. 执行 `python3 main.py` 运行同步。默认只执行一轮，适合手动运行或定时任务。
5. 如果需要像旧版本一样常驻循环运行，在 `.env` 中设置 `RUN_FOREVER=True`。当同步出现异常脚本会自动退出，所以常驻模式建议配合第三方守护进程，Linux 推荐 supervisor。

### GitHub Action 运行

1. Fork 本项目到自己的 GitHub 账号下；
2. 在 Fork 的项目中，在 “Settings - Secrets and variables - Actions” 页面添加环境变量，环境变量名如下：
   * `IGPSPORT_USERNAME`: iGPSPORT 账户名
   * `IGPSPORT_PASSWORD`: iGPSPORT 账户密码
   * `IMXINGZHE_USERNAME`: 行者账户名
   * `IMXINGZHE_PASSWORD`: 行者账户密码
   * `ONELAP_USERNAME`: 顽鹿账户名
   * `ONELAP_PASSWORD`: 顽鹿账户密码
3. 在项目的 Actions 页面，选择 “Run manually” 这个 action，点击运行。

GitHub Action 会先运行单元测试，再执行一轮同步，不会无限循环占用 runner。当前 workflow 也配置了定时任务：每天香港/北京时间 01:15 自动运行一次。

**TODO**

- [ ] 支持更多平台

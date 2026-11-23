# ride-sync

码表骑行轨迹同步工具，支持 iGPSPORT 与行者、intervals.icu 之间同步 FIT/GPX 记录。

## 运行环境

`python3`

## 使用方法

### 本地运行

1. 克隆项目到本地
2. 执行 `pip install -r requirements.txt` 安装依赖
3. 配置 `.env` 中的导入平台、导出平台以及对应平台的账号密码

- 导出平台 `EXPORT_PLATFORM`：`igpsport`
- 导入平台 `IMPORT_PLATFORM`：`imxingzhe`、`intervalsicu`，或逗号分隔的多个（如 `imxingzhe,intervalsicu`）

### 同时同步到多个平台（双写）

`IMPORT_PLATFORM` 支持逗号分隔的多个目标。例如同时发行者和 intervals.icu：

```bash
EXPORT_PLATFORM=igpsport
IMPORT_PLATFORM=imxingzhe,intervalsicu
```

游标语义是「已同步到所有目标」：一条活动只有在上面的**每个**目标都处理完（上传成功，或该目标已存在）之后才推进同步游标。任一目标报错会中断整轮且**不推进游标**，下次重跑时已成功的目标靠去重跳过、失败的目标重试——不会丢数据。

每个目标各自持有一份既有活动列表用于去重，互不干扰，因此某个目标里已有该活动不会影响另一个目标的上传。

### 同步到 intervals.icu

`IMPORT_PLATFORM=intervalsicu` 时，把 iGPSPORT 的活动直接上传到 intervals.icu，不经过 Strava：

1. 在 intervals.icu → Settings → 页面底部 **Developer Settings** 生成个人 API key；
2. 在 `.env` 中配置：

```bash
EXPORT_PLATFORM=igpsport
IMPORT_PLATFORM=intervalsicu
INTERVALS_ICU_API_KEY=你的APIkey
# 留空或填 0 即表示“该 API key 所属的运动员”，通常不必手填
INTERVALS_ICU_ATHLETE_ID=
```

3. 先只读检查差异，确认无误后再真正上传：

```bash
CHECK_ONLY=True python3 main.py
```

鉴权用 Basic，用户名固定 `API_KEY`、密码为你的 API key（这是 intervals.icu 的约定，不是本项目自创）。`INTERVALS_ICU_API_KEY` 属于机密，不要提交到 Git。

> 为什么不走 Strava 中转：Strava 的 API 条款禁止第三方应用把 Strava 来源的活动再经 API 转发出去（即使是你自己的数据），且禁止经由中介平台路由。iGPSPORT 直传 intervals.icu 可完全避开这些限制，走的是设备原始 FIT 文件。

行者 → iGPSPORT 时，建议先只读检查：

```bash
CHECK_ONLY=True python3 main.py
```

确认差异无误后，必须显式设置 `SYNC_CONFIRM=YES` 才会上传。行者若网页登录接口无法返回令牌，可在 `.env` 配置从浏览器请求中复制的 `IMXINGZHE_TOKEN`，必要时同时配置 `IMXINGZHE_COOKIE`。不要把这些值提交到 Git。

行者网页端可以免费下载 GPX，而 FIT 可能需要会员。因此行者 → iGPSPORT 默认使用 GPX；也可通过 `SYNC_FILE_FORMAT=gpx` 明确指定。其他方向默认仍使用 FIT。iGPSPORT 网页上传接口支持 `.fit`、`.gpx` 和 `.tcx`。

同步前会读取目标平台，默认按“同一天 + 里程相差不超过 2 km”跳过重复记录，并推进同步游标，避免手动导入的记录在下一次定时同步时再次生成。若两个平台都提供开始时间，还会要求开始时间相差不超过 30 分钟；活动列表只有日期时则使用日期+里程判断。可用 `DEDUP_TOLERANCE_KM` 调整里程容差。

4. 执行 `python3 main.py` 运行同步。默认只执行一轮，适合手动运行或定时任务。
5. 如果需要像旧版本一样常驻循环运行，在 `.env` 中设置 `RUN_FOREVER=True`。当同步出现异常脚本会自动退出，所以常驻模式建议配合第三方守护进程，Linux 推荐 supervisor。

### 同步后自动生成训练分析（v3）

每次同步到 intervals.icu 后，自动为新活动生成一份训练分析报告并推送（与同步通知同渠道）。
报告结构对齐 IntervalCoach 邮件（数字基于校准后的 FTP 基准）：

- 一句话总结 + 训练分析评分（透明规则：完成度50% + 心率漂移30% + 功率稳定性20%）
- 元信息行（星期 • 时长 • TSS • 刺激类型）
- 创纪录 / 历史最佳对比（5s/1min/2min/5min/20min/1h，对比"本次之前的历史最好"，给出 ΔW/Δ%）
- 体能影响（CTL →、TSB →、训练评分周均变化）、核心发现、观察
- 训练效果 / 付出 / 训练刺激 / 恢复影响 / 负荷 / 下一次训练
- 主区间行、与约 5 周前相似骑行的对比、恢复营养、建议
- 叙述部分由模型生成（openclaw capability model run --agent main），失败时退回模板

手动分析：

```bash
venv/bin/python3 ride_report.py --latest          # 最近一条
venv/bin/python3 ride_report.py --id i191195502   # 指定活动
venv/bin/python3 ride_report.py --latest --send   # 推送 Telegram
venv/bin/python3 ride_report.py --latest --no-commentary   # 不用模型
```

自动推送已接入 `sync_and_notify.sh`（新活动时 `--newly-synced 180 --send`）。
已推送过的活动记录在 `.ride_report.state.json`，不会重复推送；模型不可用自动退回模板。

```bash
RIDE_REPORT_MODEL=claude-fable-5   # 可选：固定点评模型
```

### GitHub Action 运行

1. Fork 本项目到自己的 GitHub 账号下；
2. 在 Fork 的项目中，在 “Settings - Secrets and variables - Actions” 页面添加环境变量，环境变量名如下：
   * `IGPSPORT_USERNAME`: iGPSPORT 账户名
   * `IGPSPORT_PASSWORD`: iGPSPORT 账户密码
   * `IMXINGZHE_USERNAME`: 行者账户名
   * `IMXINGZHE_PASSWORD`: 行者账户密码
3. 在项目的 Actions 页面，选择 “Run manually” 这个 action，点击运行。

GitHub Action 会先运行单元测试，再执行一轮同步，不会无限循环占用 runner。当前 workflow 默认只支持手动运行。

如果需要恢复每天香港/北京时间 01:15 自动运行，取消 `.github/workflows/run-manually.yml` 中 `schedule` 块的注释即可：

```yaml
  schedule:
    # 01:15 Asia/Hong_Kong / Asia/Shanghai, GitHub cron uses UTC.
    - cron: '15 17 * * *'
```

GitHub cron 使用 UTC，所以上面的 `15 17 * * *` 对应香港/北京时间 01:15。

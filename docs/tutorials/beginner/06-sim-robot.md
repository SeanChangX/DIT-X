# 06 模擬機器人

**目標**：以與實體機器人完全相同的方式，在你的電腦上執行整套系統，查看 topic 與日誌，並檢查介面。

**時間**：約 15 分鐘

## 背景

機器人上**不存放原始碼，也不進行編譯**：每個 domain 都是一個 Docker 容器，執行由 CI 建置的 runtime image，並以 `docker compose` 管理。`config/robots.ini` 內建一台名為 **`sim`** 的機器人，代表的就是你的電腦：

```ini
[sim]
host = local
arch = amd64
domain_id = 42
profiles = main,navigation,localization,vision
tag = develop
discovery = localhost
```

- `host = local`：狀態存放在工作區的 `.sim-robot/`，不需要 SSH
- `discovery = localhost`：DDS 只在本機探索 node，不會與實驗室網路上的實體機器人互相干擾
- `tag = develop`：使用 CI 發佈的 `develop` image（第一次會自動 pull）；改成 `local` 則使用你以 `ditx image` 建置的 image

## 前置條件

- 完成 [02 第一次建置與測試](02-first-build.md)，並已執行 `ditx image`

## 步驟

### 1. 啟動

```bash
ditx up -r sim
```

![up](../../assets/term/up.svg)

`-r sim` 可以省略，因為預設的機器人就是 `sim`（由 `.env` 中的 `DIT_ROBOT` 設定）。

### 2. 查看 topic

```bash
ditx topics -r sim
```

![topics](../../assets/term/topics.svg)

`topics` 是一個 plugin（`tools/ext/ditx-topics`），撰寫方式請參閱 [13 撰寫 plugin](../advanced/13-plugins.md)。

### 3. 與介面合約比對

```bash
ditx contract live -r sim
```

![contract-live](../../assets/term/contract-live.svg)

實際執行中的每個 `/robot/...` topic 都必須登錄於 `contract.yaml`，型別須一致，且都有 publisher。

### 4. 查看日誌

```bash
ditx logs -r sim -d main
```

![logs-main](../../assets/term/logs-main.svg)

這就是「啟動交握（handshake）」：main 的 startup 會等待每個 domain 回報 ready，全部就緒後，strategy 才開始傳送目標點。

- `-f`：持續追蹤輸出（按 ++ctrl+c++ 結束）
- `--fetch`：將整個日誌資料夾複製到 `logs/sim/`，以便離線分析

### 5. 使用 Foxglove / RViz 檢視

```bash
ditx app foxglove -r sim    # Foxglove bridge，接著用 Foxglove 或 Lichtblick 連線至 ws://localhost:8765
ditx app rviz -r sim        # RViz2 視窗
```

![app-foxglove-dry-run](../../assets/term/app-foxglove-dry-run.svg)

在 TUI 中，於 **Plugins** 分頁選取 `app`，即可從選單挑選工具與機器人。

| 工具 | 說明 |
|---|---|
| `foxglove` | Foxglove bridge，port 8765 |
| `rviz` | RViz2（X11 視窗） |
| `gazebo` | Gazebo 空白世界，用於測試 |
| `groot` / `qgc` | Groot2、QGroundControl，透過 `ROS2X app` 執行；須先將 ROS2X 複製（clone）到 `~/ROS2X` |

GUI 工具不需安裝在本機：`app` plugin 使用 [ROS2X](https://github.com/SeanChangX/ROS2X) 的 Humble desktop image，自動套用該機器人的 `ROS_DOMAIN_ID` 與 DDS discovery 設定，並掛載本工作區建置的 `eurobot_interfaces`，因此自訂型別也能正常解析。首次執行時會先下載 image（約 3 GB）。

!!! note "Jazzy 工作區"
    ROS2X 目前只提供 Humble 的 desktop image。在 Jazzy 工作區中，`std_msgs`、`geometry_msgs` 等標準訊息仍可正常顯示，但 `eurobot_interfaces` 型別無法解析。你可以透過 `DITX_APP_IMAGE=<image>` 改用其他 desktop image。

### 6. 停止

```bash
ditx down -r sim
```

`down` 執行前會詢問 `[y/N]`；在指令碼中可使用 `--yes` 略過確認。

## 摘要

- `sim` 是定義於 `config/robots.ini` 的一台「機器人」，執行方式與實體機器人相同
- `up` / `down` 用於管理整套容器，`topics` / `logs` / `contract live` 用於觀察系統狀態
- `ditx app rviz|foxglove` 透過 ROS2X 的 GUI 工具連線至機器人

## 下一步

[07 部署 overlay 與復原](../intermediate/07-deploy.md)：修改一行程式，並僅將建置產物推送至機器人。

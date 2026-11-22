# 設定檔

| 檔案 | 內容 | 納入 Git 版本控制 |
|---|---|---|
| [`ditx.repos`](#ditxrepos) | 所有 domain 及其 repo 與版本 | ✓ |
| [`config/robots.ini`](#configrobotsini) | 所有機器人、連線方式與要執行的服務 | ✓ |
| [`.env`](#env) | 你本機的預設值 | ✗（僅 `.env.example` 納入 Git） |
| [`contract.yaml`](#contractyaml) | 跨 domain 的 topic 與 QoS | ✓（位於 `base/eurobot_common/`） |
| `<domain>_bringup/config/default.yaml` | 每個 node 的參數 | ✓（位於各 domain repo） |
| `<domain>_bringup/config/robots/<robot>.yaml` | 特定機器人的參數覆寫 | ✓（位於各 domain repo） |

## `ditx.repos`

此檔案採用 [vcstool](https://github.com/dirk-thomas/vcstool) 格式，因此也可以使用 `vcs import < ditx.repos`。`ditx sync` 會依據此檔案將各 domain 複製（clone）或 fast-forward 至 `domains/<name>`。

```yaml
repositories:
  domains/main:
    type: git
    url: https://github.com/SeanChangX/Eurobot-Main.git
    version: develop
  domains/navigation:
    type: git
    url: https://github.com/SeanChangX/Eurobot-Navigation.git
    version: develop
  # ... localization, vision
```

- `version`：開發期間設為分支；在 release 分支上則固定為 tag 或 commit。shell repo 的 tag 加上此檔案，即可完整重現機器人軟體。
- 若要暫時切換某個 domain 的分支，不必修改檔案：`ditx sync --ref localization=feat/ekf`（CI 中使用 `DIT_REF_LOCALIZATION=feat/ekf`）。
- 若要改用另一份檔案（例如測試用的本機 repo）：`DIT_REPOS_FILE=/path/to/other.repos`。

## `config/robots.ini`

每個區段代表一台機器人，`ditx <指令> -r <名字>` 會指向該機器人。你可以使用 [`ditx robot add/remove`](../tutorials/intermediate/10-robots.md) 管理，也可以直接編輯檔案（註解會保留）。

```ini
[sim]
host = local
arch = amd64
domain_id = 42
profiles = main,navigation,localization,vision
tag = develop
discovery = localhost
note = everything on this laptop, state under .sim-robot/
```

| 鍵 | 必填 | 說明 |
|---|---|---|
| `host` | ✓ | 機器人的 hostname（DIT 機器人以你的 `DIT_ROBOT_USER` 登入），或 `user@host`、`~/.ssh/config` 中的 Host 別名；`local` 表示本機 |
| `arch` | | `amd64`（預設）或 `arm64`；overlay 會以此架構建置 |
| `domain_id` | ✓ | 此機器人上所有程式使用的 `ROS_DOMAIN_ID`，每台機器人必須不同 |
| `profiles` | | 要執行 `docker/compose.yaml` 中的哪些 service：domain 名稱，以及 `firmware`（micro-ROS agent）、`monitoring`（netdata） |
| `tag` | | image tag：`stable`（main）、`develop`、`vX.Y.Z`，或 `local`（由 `ditx image` 建置） |
| `discovery` | | `subnet`（預設）或 `localhost`：DDS 只在本機探索，因此 sim 不會與實驗室中的實體機器人互通 |
| `note` | | 顯示用的備註 |

實體機器人的範例：

```ini
[white]
host = dit-2026-11.local
arch = amd64
domain_id = 11
profiles = main,navigation,localization,vision,firmware,monitoring
tag = stable
```

## `.env`

此檔案存放你本機的設定，**不納入 Git**。執行 `ditx init` 依提示逐項設定，或複製 `.env.example`：

```bash
# Defaults for ditx. Copy to .env (git-ignored) or run `ditx init` to change them.
ROS_DISTRO=humble
REGISTRY=ghcr.io/seanchangx
# dev image tag: develop (default) = pull the image CI publishes from the develop branch;
# local = built on this machine by `ditx image` (after changing the Dockerfile or dependencies)
DIT_DEV_TAG=develop
# robot used when -r is not given
DIT_ROBOT=sim
# your login on DIT-Scripts robots: your team's account (main, navigation, localization, vision)
DIT_ROBOT_USER=main
```

優先順序為 **環境變數 > `.env` > `.env.example` > 內建預設值**，因此 `DIT_ROBOT=white ditx logs` 只會影響該次執行。

| 變數 | 預設 | 說明 |
|---|---|---|
| `ROS_DISTRO` | `humble` | `humble`（機器人使用的版本）或 `jazzy`（未來支援） |
| `REGISTRY` | `ghcr.io/seanchangx` | image 名稱的前綴（目前 image 只在本機建置，不會下載） |
| `DIT_DEV_TAG` | `develop` | dev 與 cross image 的 tag：`develop` 使用 CI 發佈到 GHCR 的 image（自動 pull）；`local` 使用在本機以 `ditx image` 建置的 image，修改 Dockerfile 或相依套件時使用 |
| `DIT_DEV_IMAGE` | | 直接指定完整的 dev image 名稱（覆寫上述兩項） |
| `DIT_ROBOT` | `sim` | 未指定 `-r` 時使用的機器人 |
| `DIT_ROBOT_USER` | `main` | 你在機器人上的帳號，也就是你的小組（`main`、`navigation`、`localization`、`vision`）；`robots.ini` 中沒寫帳號的 DIT 機器人都用它登入 |
| `DIT_REPOS_FILE` | `ditx.repos` | 改用另一份 repos 檔案 |

以下設定只能透過環境變數指定：

| 變數 | 說明 |
|---|---|
| `NO_COLOR=1` | 停用色彩輸出 |
| `CI=1` | CI 模式：顯示完整輸出、不顯示 spinner（GitHub Actions 會自動設定） |
| `DITX_NO_MOUSE=1` | 啟動 TUI 時即停用滑鼠（等同按下 `m`） |
| `DIT_REF_<DOMAIN>` | 等同 `ditx sync --ref <domain>=<ref>` |

## `contract.yaml`

`base/eurobot_common/contract.yaml` 定義所有**跨 domain** 的 topic 與 service，詳細教學請參閱 [05](../tutorials/beginner/05-contract.md)。

```yaml
version: 1

domains: [main, navigation, localization, vision]

qos:
  sensor:  {reliability: best_effort, durability: volatile,        history: keep_last, depth: 1}
  state:   {reliability: reliable,    durability: transient_local, history: keep_last, depth: 1}
  command: {reliability: reliable,    durability: volatile,        history: keep_last, depth: 10}

topics:
  LOCALIZATION_POSE:
    name: /robot/localization/pose
    type: geometry_msgs/msg/PoseWithCovarianceStamped
    qos: sensor
    publisher: localization
    subscribers: [navigation, main]
    rate_hz: 50
    doc: Fused robot pose in the map frame. header.stamp = time of the newest fused measurement.
```

| 欄位 | 說明 |
|---|---|
| `domains` | 系統可識別的 domain；啟動交握會拒絕不在此清單中的 domain |
| `qos` | 三種 QoS profile；每個 topic 必須從中選用一種，請勿自行定義 |
| `topics.<KEY>` | KEY 會成為程式中的常數：`contract.LOCALIZATION_POSE` / `eurobot_common::contract::LOCALIZATION_POSE` |
| `name` | 格式為 `/robot/<區域>/<東西>`，使用 snake_case；區域為擁有該 topic 的 domain，或 `startup` / `system` |
| `type` | message 型別，請盡量使用標準型別 |
| `publisher` | 負責發布的 domain；`"*"` 表示每個 domain |
| `subscribers` | 訂閱的 domain；`["*"]` 表示全部 |
| `rate_hz` | 預期頻率（僅供文件使用） |
| `doc` | 一句話的說明，會顯示於 [Interface contract](../contract.md) |

僅在單一 domain 內部使用的 topic **請勿**加入此檔案。修改此檔案會改變 base 的指紋（fingerprint），請參閱 [11](../tutorials/advanced/11-interfaces.md)。

| 指令 | 作用 |
|---|---|
| `ditx contract` | 驗證格式 |
| `ditx contract md` | 重新產生 `docs/contract.md` |
| `ditx contract live -r <robot>` | 與執行中的機器人比對 |

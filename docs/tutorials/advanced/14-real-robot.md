# 14 arm64 與實體機器人

**目標**：設定一台新的機器人，讓 `ditx up` 與 `ditx deploy` 能透過 SSH 操作它；若機器人使用 ARM 開發板，還需要能進行交叉編譯。

**時間**：約 30 分鐘（首次設定）

!!! warning "尚未在實體機器人上驗證"
    本章的流程與 `sim` 使用相同的程式碼，只是執行層從本機 Bash 改為 SSH。但目前**尚未在實體機器人上測試**：arm64 的交叉編譯產物已在模擬器中確認可以執行，但尚未部署到真正的 Raspberry Pi 或 Jetson 上。以下輸出來自 `--dry-run` 與本機執行結果；若首次操作時遇到問題，請回報並修正本文件。

## 背景

機器人上只需要 Docker、[DIT-Scripts](https://github.com/DIT-ROBOTICS/DIT-Scripts) 建立的帳號，以及 `infra/robot/install.sh` 建立的資料夾與 systemd timer。**不需要** ROS、原始碼或編譯器。

DIT-Scripts 在每台機器人上建立以下帳號，全部都在 `docker` 群組裡：

| 帳號 | 用途 |
|---|---|
| `ditrobotics` | 桌面與管理帳號，負責執行 `install.sh` |
| `main`、`navigation`、`localization`、`vision` | 各小組自己的帳號，也就是你在 `ditx init` 設定的帳號。用任何一個都能部署自己的 domain，或啟動整台機器人 |

`ditx` 在機器人上的所有東西都集中在 `/home/share/eurobot/`，跟 DIT-Scripts 的 `/home/share/data`、`/home/share/scripts` 放在一起。本機的 `sim` 機器人在 `.sim-robot/` 裡也是同一套結構：

| 路徑（`/home/share/eurobot/` 之下） | 用途 |
|---|---|
| `compose.yaml`、`.env` | 由 `ditx` 寫入，請勿手動修改 |
| `overlays/` | `ditx deploy` 推送的建置產物 |
| `run/active/` | 目前啟用的 overlay，**每次開機都會清空**，因此重新開機後一定回到穩定版 |
| `active/` | 指定 `--persist` 時改為記錄於此，重新開機後仍然有效 |
| `logs/` | 各容器的日誌，以及 `system/`（系統日誌、kernel 訊息） |
| `config/logging.env` | 日誌保留天數、容量上限、NAS 同步設定 |

整個資料夾的擁有者是 `ditrobotics`，群組是 `docker`（設定了 setgid），容器也以同一個群組執行。因此不論用哪個帳號部署，都能覆蓋或清理其他帳號推送的 overlay，不會遇到權限問題。容器也會加入 `dialout` 群組，所以不論誰部署，都能開啟 `/dev/dit-*` 序列埠裝置。

## 前置條件

- 已完成 [10 管理機器人](../intermediate/10-robots.md)
- 機器人已用 DIT-Scripts 設定好（Ubuntu、Docker、各組帳號），且你可以透過 SSH 登入

## 步驟

### 1. 設定 SSH 金鑰

在**你的電腦**上執行（以下以 `main` 為例，請換成你在 `ditx init` 設定的小組帳號）：

```bash
ssh-copy-id main@dit-2026-11.local
ssh main@dit-2026-11.local echo ok        # 沒有詢問密碼就印出 ok，代表金鑰設定成功
```

DIT-Scripts 已經將這些帳號加入 `docker` 群組，所以 `ssh main@dit-2026-11.local docker ps` 不需要 sudo 就能執行，這是 `ditx` 的必要條件。小組帳號（例如 `vision@dit-2026-11.local`）也用同樣方式設定金鑰。

### 2. 在機器人上執行 install.sh

在**機器人上**以 `ditrobotics` 帳號登入（桌面或 SSH 皆可），複製（clone）shell repo 後執行：

```bash
sudo infra/robot/install.sh --dry-run     # 先看會做什麼
sudo infra/robot/install.sh
```

![robot-install-dry-run](../../assets/term/robot-install-dry-run.svg)

- 此指令可重複執行，只會修正偏離預期的部分；`--uninstall` 會移除 timer 與指令碼，但保留資料。
- 資料夾的擁有者是執行 `sudo` 的那個帳號（`ditrobotics`），若要指定其他帳號，請加上 `--user <name>`。`install.sh` 也會確認所有 DIT-Scripts 帳號都在 `docker` 與 `dialout` 群組中。
- 請先修改 `infra/robot/udev/` 中的序號預留位置，`/dev/dit-lidar` 這類固定裝置名稱才會生效。
- **務必安裝 chrony**：多台電腦的日誌時間戳記必須一致。

### 3. 準備機器人的 image

CI 會把 runtime image 發佈到 GHCR（`main` → `:stable`、`develop` → `:develop`、release tag → `:vX.Y.Z`，amd64 與 arm64 都有），所以機器人可以直接 pull，不必建置：從你的電腦執行 `ditx up -r white --pull` 即可。

若要執行還沒發佈的版本（例如自己的分支），才在機器人上自行建置。arm64 機器人也是在機器人上原生建置，不需要 qemu。在**機器人上**，於第 2 步的 shell repo 中執行：

```bash
git switch main && git pull     # 比賽用 main（tag = stable）；測試機可用 develop
bin/ditx sync
bin/ditx image --target runtime --tag stable    # tag 與 robots.ini 中的 tag 相同
```

之後每次要讓機器人執行新版本，都在機器人上重複這三行。機器人的 `ditrobotics` 帳號需要能讀取這些 repo，例如設定一把 SSH 金鑰並加到 GitHub。

### 4. 從你的電腦檢查並啟動

```bash
ditx doctor -r white
ditx up -r white
ditx contract live -r white
```

之後 `deploy`、`rollback`、`logs`、`overlays` 的用法與 [07](../intermediate/07-deploy.md)、[08](../intermediate/08-debugging.md) 完全相同，只需將 `-r sim` 改為 `-r white`。

### 5. arm64 機器人（Raspberry Pi、Jetson）

登錄機器人時指定架構：

```bash
ditx robot add pi --host DIT-2026-12 --arch arm64
```

在 x86 電腦上建置 arm64 版本採用**交叉編譯**：`eurobot-cross` image 內含 aarch64 編譯器，以及一份 arm64 的 sysroot（ROS、各 domain 的相依套件、`base/`）。編譯在你的電腦上以原生速度進行，**不需要安裝 qemu**。

`ditx deploy -r pi` 會自動以 `--arch arm64` 建置，產物存放在 `build/humble-arm64/`，與 amd64 的產物分開。你也可以手動建置：

```bash
ditx build --arch arm64 -d localization
```

![build-arm64](../../assets/term/build-arm64.svg)

!!! note "arm64 建置不包含測試"
    交叉編譯出來的測試程式無法在 x86 電腦上執行，因此 `--arch arm64` 會略過測試的建置。測試請照常使用 `ditx test`（在本機架構執行）；程式邏輯與架構無關，兩邊結果相同。

!!! info "運作原理"
    - sysroot 由 `docker/cross/make-sysroot.sh` 建立：直接下載 arm64 的 .deb 並解開，過程中不執行任何 arm64 程式，因此建 image 也不需要 qemu。
    - `docker/cross/aarch64.cmake` 是 CMake toolchain file，讓 colcon 使用 aarch64 編譯器，並從 sysroot 尋找標頭檔與函式庫。
    - arm64 機器人的 runtime image 在機器人上原生建置（第 3 步），同樣不需要 qemu。
    - 只有 `ditx shell --arch arm64`（實際執行 arm64 程式）需要 qemu，`ditx doctor` 會列出安裝方式。

## 摘要

- 機器人只需要 Docker 與 `infra/robot/install.sh`，不存放原始碼。
- 設定流程：`ditx robot add` → 在機器人上執行 `install.sh` 並建置 image → `ditx doctor -r` → `ditx up -r`。
- arm64：使用 `--arch arm64`，以交叉編譯建置，不需要 qemu。

## 下一步

[15 C++ 開發環境](15-cpp.md)

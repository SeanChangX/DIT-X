# 疑難排解 FAQ

請先執行 `ditx doctor`。此指令會檢查 Docker、磁碟、RAM、image、domain 與機器人連線，每個 `✗` / `!` 後方都會列出修正方式。

## 安裝與環境

??? question "`ditx: command not found`"
    在工作區根目錄執行 `./install.sh`，然後開啟新的終端機（或執行 `exec $SHELL`）。安裝前也可以直接使用 `bin/ditx`。

??? question "`ditx cd` 沒有切換目錄"
    程式無法變更 shell 的目前目錄，因此 `ditx cd` 需要透過 `./install.sh` 安裝的 shell function 運作。若尚未安裝，工具會提示你改用 `cd "$(ditx cd localization)"`。

??? question "`permission denied while trying to connect to the Docker daemon`"
    將你的帳號加入 docker 群組，然後**登出再重新登入**：

    ```bash
    sudo usermod -aG docker $USER
    ```

??? question "`dev image ghcr.io/seanchangx/eurobot-dev:local is not built yet`"
    你設定了 `DIT_DEV_TAG=local`（使用自己建置的 image），但還沒建置。建置一次即可（約 5–10 分鐘）：

    ```bash
    ditx image
    ```

    不需要修改 Dockerfile 或相依套件時，刪掉 `.env` 中的 `DIT_DEV_TAG`，改用 CI 發佈的 image。

??? question "`the dev image ... lacks dependencies of this checkout`"
    `ditx build` 會先自動 pull 最新的 dev image 再檢查一次（別人合併的相依套件，新的 image 可能已經有了）。仍然出現這個訊息，代表是你的分支新增了相依套件，而 CI 發佈的 dev image 是依照 `develop` 建置的，還沒有這些套件。改用自己建置的 image：

    ```bash
    ditx image --target dev
    DIT_DEV_TAG=local ditx build
    ```

    分支合併進 `develop` 後，CI 發佈的新 image 就會包含這些套件。

    `cross image ... is not built yet`（`ditx build --arch arm64`）同理，執行 `ditx image --target cross`。

??? question "`disk: 18 GB free → a bit tight`"
    image 加上建置產物約需 20 GB。執行 `ditx clean` 清除建置產物；執行 `docker image prune` 清除未使用的舊 image。

??? question "建置時整台電腦停止回應"
    正常情況下不會發生：`-j auto` 會依 RAM 決定平行數量，並將容器的記憶體上限設為 80%，最壞情況只會終止建置。若仍然過於緩慢，請手動降低平行數量：`ditx build -j 2 --workers 1`。

## 建置與測試

??? question "修改程式後，測試或機器人上的行為仍是舊版"
    這幾乎都是因為忘了重新建置。`ditx test` 與 `ditx deploy` 使用的是 `build/` 中的結果：

    ```bash
    ditx build -d vision && ditx test --integration
    ```

    整合測試出現 `FAIL no publisher on /robot/...` 時，通常就是這個原因。

??? question "`base changed (… → …), rebuilding it from scratch`"
    這是正常訊息：`base/` 的介面已變更，依賴它的 domain 必須完整重新建置。請參閱 [11 修改共用介面](../tutorials/advanced/11-interfaces.md)。

??? question "`using a LOCAL base build`"
    你先前執行過 `ditx build --base`，目前使用的是本機建置的 base，而非 image 中的版本。不再需要時，執行 `ditx clean --base --yes`。

??? question "lint 失敗：`I001 Import block is un-sorted`、clang-format 差異"
    格式問題皆可自動修正：執行 `ditx lint --fix`，再以 `git diff` 檢視變更內容。

??? question "Jazzy 的 CI 失敗"
    Jazzy 是下一個 ROS 版本，CI 只用它提早發現相容性問題，允許失敗，不會阻擋合併。機器人實際使用的 Humble 則必須通過。

## 機器人

??? question "`interfaces differ from the robot image`"
    你的 overlay 是以不同的介面版本（base fingerprint）建置的，請參閱 [11](../tutorials/advanced/11-interfaces.md)。正確做法是合併介面的 PR 後，在機器人上重新建置 image（[14](../tutorials/advanced/14-real-robot.md) 第 3 步）；`--allow-skew` 僅限測試時使用。

??? question "deploy 輸出 `did not come up` 後自動復原"
    node 啟動後當掉。`ditx` 已輸出容器的日誌；若要保留故障現場以便深入調查，執行 `ditx deploy -d <domain> -r <robot> --no-rollback`，再以 `ditx logs -r <robot> -d <domain>` 檢視日誌。

??? question "overlay 名稱後方出現 `-dirty`"
    表示部署時有尚未提交的修改。部署仍可進行，但事後無法確認實際執行的是哪個版本的程式碼；正式測試前請先提交。

??? question "機器人重新開機後，我的 overlay 消失了"
    這是預期行為：重新開機後一律回到 CI 建置的穩定版。若要保留 overlay，請使用 `ditx deploy --persist`。

??? question "`robot white (…) not reachable`"
    機器人未開機、不在同一個網路，或尚未設定 SSH 金鑰。請先執行 `ssh main@dit-2026-11.local echo ok` 測試連線：沒有詢問密碼就印出 `ok`，代表連線與金鑰都正常。

??? question "筆電啟用 UFW 防火牆後，`ros2 topic list` 看不到機器人的 topic"
    UFW 預設為 `deny (incoming)`。SSH、部署、檢視日誌、sim，以及 `ditx app` 連線至 sim 都不受影響（這些都是由筆電主動對外連線，或只經過 loopback）；但機器人送出的 DDS discovery multicast
    與資料封包不屬於「回應」，會遭到封鎖，因此筆電上的 `ros2 topic echo` 與 RViz 收不到機器人的資料。請只允許該台機器人的流量（`ditx doctor -r <robot>` 會輸出含 IP 的指令）：

    ```bash
    sudo ufw allow from <機器人 IP> proto udp comment 'ditx white DDS'
    ```

    請勿完全停用 UFW，也不要對整個網段開放所有 port。

??? question "sim 是否會與實驗室中的實體機器人互相干擾？"
    不會。`sim` 設定了 `discovery = localhost`，DDS 只在本機探索節點，且其 domain ID（42）也與實體機器人不同。在 `ditx shell` 中自行執行 node 並要連上 sim 時，請先執行 `export CYCLONEDDS_URI=file:///opt/eurobot/cyclonedds-localhost.xml`（Humble 與 Jazzy 皆適用；Humble 的 `ROS_LOCALHOST_ONLY` 無法與 sim 互通）。

## TUI

??? question "無法在 TUI 中選取或複製文字"
    TUI 會攔截滑鼠事件。按下 ++m++ 停用滑鼠（右下角會顯示 `MOUSE OFF`），或按住 ++shift++ 拖曳選取。若要在啟動時即停用滑鼠，請執行 `DITX_NO_MOUSE=1 ditx`。

??? question "避免誤執行指令"
    在 TUI 中按下 ++n++ 切換為 dry-run 模式；在 CLI 中則加上 `-n`。此模式只會輸出將要執行的內容，不會做任何變更。

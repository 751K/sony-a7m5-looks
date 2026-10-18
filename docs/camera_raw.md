# Sony A7M5 Creative Look → Camera Raw 配置（进行中）

> 研究记录，按时间顺序保留了当时的推断和更正。文中提到的测试导出、`c1_test/`、`photo_test/`、`chart/` 测量结果、REA 证据和反编译文件属于原研究工作区，不在本仓库中；脚本名和用法以仓库根目录 README 为准。

## 思路

- **底座 DCP**（`SonyA7M5 Linear Base.dcp`）：
  - ColorMatrix1/2 沿用 Adobe Standard，白平衡行为与 Adobe 一致。
  - ForwardMatrix = 线性 ProPhoto → XYZ D50，所以 ACR 的工作 RGB 就是白平衡后的相机 RGB。
  - 影调曲线为恒等，不加暗部处理（DefaultBlackRender = none），没有 HueSatMap / LookTable。
- **外观**：建在底座上的 XMP "Look" 配置，带一张 RGBTable（3D LUT，ProPhoto 原色、γ1.8 编码）。
- 和 C1 一样，先用探针配置在合成色卡上测出 ACR 送进表的实际值，再生成外观表。

## 格式（`crs_table.py`、`acr_profiles.py`）

- `crs:Table_<MD5>` 字符串：Adobe 字母表的 base-85（小端，4 字节 → 5 字符），内容是 `[u32 原长][zlib]`。MD5 是解压后数据的 MD5。用 Adobe 自带配置验证过：解码后的 MD5 等于表名。
- RGBTable 二进制：`u32 1, u32 1, u32 3, u32 n`；然后 n³ 个节点，每个节点是 (R,G,B) u16，存的是"值 − 恒等值"，按 mod 2¹⁶ 回绕。节点顺序为 R 最慢、B 最快，恒等值 = round(i·65535/(n−1))。最后是 `u32 原色, u32 gamma, u32 gamut, f64 最小强度, f64 最大强度`。
- 枚举值是从 Adobe 配置中实际出现的组合推断的：(0,1) sRGB/sRGB 曲线，(1,3) Adobe RGB/γ2.2，(2,2) ProPhoto/γ1.8。Adobe 自带配置的 n 有 16/20/25/32 几种。
- DCP：`dcp.py` 读写（IIRC TIFF 容器）。

## 安装位置

- DCP：`~/Library/Application Support/Adobe/CameraRaw/CameraProfiles/`
- XMP：`~/Library/Application Support/Adobe/CameraRaw/Settings/`
- 改动后需要重启 Photoshop。

## 探针

- `Probe`：恒等表，导出值 = 送进表的值。
- `Probe-Sq`：输出 = 输入²，用来检查表之后是否还有处理。
- `Probe-Mono`：输出 = ProPhoto 亮度，用来检查是否额外加了颜色。
- 测量：`measure_acr_chart.py`（读导出的原始编码值，配准方法同 C1）。

## 探针结果（2026-10-16，`chart/measure_SonyChart{probe,sq,mono}.json`）

ACR 设置：PV 15.4，白平衡"拍摄时设置"，镜头校正和色差校正关闭，其余默认。三张导出都是 ProPhoto 16 位，配准 249–263 个色块。

- **Probe-Sq = Probe²**：平均 0.05、最大 0.10 级（8 位）。RGBTable 是最后一步，编码确实是 ProPhoto γ1.8，表之后没有别的处理。
- **Probe-Mono = 亮度**：平均 0.003 级。ACR 不会额外加颜色。
- **送进表的值**（`acr_encoding.py` → `acr_encoding.json`）：
  - 白平衡与 Sony 一致，灰阶三通道相等。
  - 中间调严格线性：x = v^1.8 = Sony 线性 / 6422。线性值 1500 以上有肩部压缩，传感器饱和点对应 v = 0.905。线性值 5 以下有轻微的暗部压暗。
  - **压缩是保色相的**：最大和最小通道走同一条 1D 曲线，中间通道在线性空间保持比例，同 DNG SDK RefBaselineRGBTone。这个模型对 264 个色块的误差：平均 0.04、最大 0.37 级（8 位）。只用逐通道模型时，饱和高光的误差可达 19 级。
- 外观表（`acr_look.py`）的每个节点：先用 `inverse(v)` 反算出 Sony 线性值，再走外观链，然后 sRGB → ProPhoto γ1.8。

照片上的离线插值误差（scale 4，8 位 MAE，四面体插值 / 三线性插值）：

| 网格 | ST | FL2 | FL3 |
| --- | --- | --- | --- |
| 32 | 0.42 / 0.53 | 0.61 / 0.80 | 0.85 / 0.97 |
| 48 | 0.26 / 0.32 | — | 0.61 / 0.66 |
| 64 | 0.20 / 0.24 | — | 0.46 / 0.51 |

Adobe 自带配置最大只用到 32；ACR 是否接受 64 还需要用 `Look ST g64` 实测。

## 色卡验证（2026-10-16，外观导出 `../c1/chart/SonyChart_{ST,STg64,FL2,FL3}.TIF`）

8 位 MAE：

| | ST 32³ | ST 64³ | FL2 32³ | FL3 32³ |
| --- | --- | --- | --- | --- |
| ACR 实际 vs 离线预测 | 0.03 | 0.03 | 0.04 | 0.04 |
| ACR 实际 vs Sony 外观目标 | 0.37（最大 13.7） | **0.14**（最大 4.5） | 0.40 | 0.36 |

- 预测与实际几乎一致，说明编码模型和表格式都理解正确。剩下的误差全部来自网格插值。
- **ACR 接受 64³ 网格**，所以现在默认网格改为 64（`acr_look.py --grid 64`），每个 XMP 约 1.8 MB。
- 注意：ACR 把导出存在 ARW 所在的目录。

## 照片验证（2026-10-16，`photo_test/DSC03591_{ST,FL2,FL3}.TIF`，64³）

- **必须关闭"使用配置文件校正"**。开着时 ACR 会做暗角补偿：中心与 Edit 的亮度比为 1.004，四角为 1.2–1.27，整体偏亮约 3 级。Edit 没有做暗角补偿。
- 关闭校正后桶形畸变还在，仿射配准对不齐，所以 `compare_acr_export.py` 改用三次多项式变形（约 100 个点，残差 1.9 px）。

| 外观 | ACR vs Edit | 对照：C1 vs Edit | 外观链 vs Edit |
| --- | --- | --- | --- |
| ST | **1.27**（p95 3.3，偏差 +0.5/+0.7/+0.6） | 1.17 | 0.90 |
| FL2 | **1.15** | 1.05 | 0.89 |
| FL3 | **1.27** | 1.21 | 1.00 |

- 和 C1 的水平相当。G 通道整体偏 +0.5–0.8 级。中间调主要是 Edit 自身偏暗（见下文）；亮区是 Camera Raw 按画面自适应的高光压缩（`pipeline.md` §11）。比外观链多出的误差来自 ACR 自己的去马赛克、锐化（默认 40）和降噪。

## 用法

1. `acr_look.py --write` 生成外观。复制 `profiles/SonyA7M5 Linear Base.dcp` 到 `~/Library/Application Support/Adobe/CameraRaw/CameraProfiles/`，复制 `profiles/SonyA7M5 Look *.xmp` 到 `~/Library/Application Support/Adobe/CameraRaw/Settings/`，然后重启 Photoshop / Lightroom。
2. 在配置文件浏览器的 "Sony A7M5" 组里选外观。不要叠加别的影调曲线；如果要和 Sony 原片一致，关闭"使用配置文件校正"里的暗角补偿。
3. 曝光、白平衡照常调整。配置只替代 Sony 的外观色彩，不包含 DRO、锐化、降噪。

### "G 偏高"的分解（ST，8 倍缩小，Y 15–240）

ACR − Edit = (+0.17, +0.54, +0.58)，拆成两部分：
1. **Edit 自身偏暗约 0.5 级**：Edit − 逐像素预测 = (−0.57, −0.46, −0.32)，三通道一致，暗部约 −1。这是 Edit 空间处理（锐化、Marble 等）带来的已知残差，不是 ACR 的问题。C1 vs Edit 的正偏差 (+0.15, +0.63, +1.09) 也包含这一部分。
2. **ACR 色度略低**：ACR − 预测 = (−0.40, +0.08, +0.26)。场景整体偏暖，色度降低就表现为 R 降、B 升。

按纹理分段，色度相对预测的比例：

| | 平坦（块内标准差 < 2） | 中等 | 纹理（≥ 4） |
| --- | --- | --- | --- |
| ACR | 0.993 | 0.970 | 0.832 |
| Edit | 1.001 | 0.972 | 0.857 |

- 平坦区两者都与预测一致，所以配置本身没有偏色，这与色卡结果相符。
- 纹理区的降低大部分是预测方法本身的问题：预测是先对线性值做块平均、再过外观链，而实际渲染是逐像素渲染后再平均。
- ACR 只比 Edit 多降了约 2–3%。**不是彩色降噪造成的**：用降噪 = 0 导出（`photo_test/DSC03591_STnoise.TIF`）后，各项结果都不变（MAE 1.272 → 1.260，纹理区色度比例 0.832 → 0.832）。剩下的可能原因是 ACR 的去马赛克或锐化，影响在 0.5 级以内，不再继续追查。

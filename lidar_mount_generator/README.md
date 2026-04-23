# LiDAR Mount Plate Generator

LiDARセンサー用の**三脚固定マウントプレート**を自動生成するPythonアプリケーションです。  
STP/STEPファイルを読み込んでネジ穴を自動検出するか、寸法を手動入力することで、3Dプリント対応のSTL/STEPファイルを出力します。

---

## 目次

1. [システム概要](#システム概要)
2. [生成される固定部材の仕様](#生成される固定部材の仕様)
3. [対応ファイルフォーマット](#対応ファイルフォーマット)
4. [動作環境・セットアップ](#動作環境セットアップ)
5. [起動方法](#起動方法)
6. [UIの使い方（3つのモード）](#uiの使い方3つのモード)
7. [設計パラメータ一覧](#設計パラメータ一覧)
8. [トラブルシューティング](#トラブルシューティング)

---

## システム概要

```
LiDAR の STP ファイル  ──→  穴位置の自動検出  ──→  マウントプレート生成  ──→  STL / STEP
         ↑                       ↓検出失敗
  手動入力モードで               パラメトリック
  穴座標を指定                   モードに切替
```

### 何をするツールか

| 機能 | 内容 |
|---|---|
| STP解析 | OpenCASCADE (OCC) エンジンでSTPを読み込み、底面付近の円形エッジ（ネジ穴）を自動検出 |
| 穴位置の正規化 | 検出した穴群の重心を原点に揃え、プレートを対称に配置 |
| ボス（スペーサー）生成 | 各ネジ穴位置に円柱状の突起を配置し、LiDARとプレートの間に放熱ギャップを確保 |
| 三脚穴 | 底面中央に 1/4インチ (1/4-20 UNC) の貫通穴と六角ナット埋め込み用ポケットを配置 |
| 出力 | Bambu Studio / PrusaSlicer / Fusion 360 で読み込める STL または STEP を生成 |

### 対応センサー

| センサー | 対応方法 |
|---|---|
| **Falcon K2** （またはSTPファイルがある任意のLiDAR） | STP自動解析モード |
| **Ouster OS2** （仕様書PDFから寸法を確認後） | パラメトリックモードで穴座標を入力 |
| その他任意のLiDAR | パラメトリックモードでピッチ・穴座標を入力 |

---

## 生成される固定部材の仕様

```
      LiDAR（上）
  ────●────────●────  ← LiDAR 底面（不均一でも対応）
      │        │
   [boss]   [boss]    ← ボス（スペーサー）: LiDARネジ穴ごとに高さが可変
      │        │         放熱ギャップ（デフォルト 3 mm）を確保
  ════════════════════ ← プレート上面  Z = plate_thickness
  │                  │
  │  ⬡ 六角ナット穴  │ ← 底面から 5 mm の六角形ポケット（1/4-20 ナット埋め込み用）
  │  ● 1/4" 三脚穴  │ ← 貫通穴 φ6.5 mm
  │                  │
  ════════════════════ ← プレート底面  Z = 0（三脚マウント面）
```

| 部位 | デフォルト値 | 変更オプション |
|---|---|---|
| プレート厚さ | 10 mm | `--plate-thickness` |
| プレート外形 | 穴配置 + 余白 10 mm | `--plate-margin` |
| コーナーフィレット | R3 mm | コード内 `Config` クラス |
| ボス高さ（最小値） | 3 mm | `--boss-height` |
| 三脚穴径 | φ6.5 mm（1/4-20 クリアランス） | `--tripod-hole-dia` |
| 六角ナットポケット | AF=11.5 mm、深さ 5 mm | コード内 `Config` クラス |

---

## 対応ファイルフォーマット

### 入力

| 形式 | 拡張子 | 備考 |
|---|---|---|
| STEP / STP | `.stp` `.step` | ISO 10303 規格。CADソフトからの標準出力 |

### 出力

| 形式 | 拡張子 | 用途 |
|---|---|---|
| **STL** | `.stl` | 3Dプリンター（Bambu Studio / PrusaSlicer / Cura）で直接読み込み可能 |
| **STEP** | `.step` | Fusion 360 / FreeCAD など CAD での後編集が必要な場合 |

> **推奨**: 3Dプリントには STL、CAD での修正が必要な場合は STEP を選択してください。  
> 出力形式はファイル名の拡張子で自動判定されます（例: `mount.stl` → STL形式）。

---

## 動作環境・セットアップ

### 必要環境

- Python **3.9 以上**
- pip（パッケージマネージャー）
- インターネット接続（初回インストール時のみ）

### インストール手順

```bash
# 1. リポジトリをクローン（または lidar_mount_generator フォルダをコピー）
git clone <このリポジトリのURL>
cd lidar_mount_generator

# 2. 仮想環境を作成（推奨）
python -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows（PowerShell）
.\venv\Scripts\Activate.ps1

# 3. 依存ライブラリをインストール
#    CadQuery は OpenCASCADE (OCC) を内包しているため、別途 OCC のインストールは不要です
pip install -r requirements.txt
```

> **注意**: CadQuery は OpenCASCADE を同梱しているため、インストールサイズが約 1 GB になります。  
> 初回のインストールには数分かかります。

### STPファイルの配置

```
lidar_mount_generator/
├── mount_generator.py
├── requirements.txt
├── README.md
└── FalconK2.stp        ← ここに STP ファイルを置く（パスは任意）
```

STPファイルはどこに置いても構いません。起動時にパスを指定します。

---

## 起動方法

スクリプトは以下の3つのモードで起動できます。

### モード① 対話型ウィザード（引数なし）

```bash
python mount_generator.py
```

起動すると対話形式でモードを選択できます（後述の「UIの使い方」を参照）。

### モード② CLIオートモード（STPファイルあり）

```bash
python mount_generator.py -i FalconK2.stp -o mount.stl
```

### モード③ CLIパラメトリックモード（寸法を手動指定）

```bash
python mount_generator.py -p --pitch 38.5 --screw-dia 3.0 -o mount.stl
```

---

## UIの使い方（3つのモード）

### 対話型ウィザード（引数なしで起動した場合）

```
==================================================
  LiDAR Mount Plate Generator
==================================================

  [1] Auto mode    – parse STP file, detect holes
  [2] Parametric   – enter screw positions manually

Select mode [1/2]:
```

**[1] を選択した場合（自動検出）**

```
Select mode [1/2]: 1
STP file path: FalconK2.stp
Output path [lidar_mount.stl]: falcon_mount.stl
```

→ STPを解析し、底面のネジ穴を自動検出してプレートを生成します。  
→ 検出された穴が2個未満の場合は自動的にパラメトリックモードに切り替わります。

**[2] を選択した場合（手動入力）**

```
Select mode [1/2]: 2
Output path [lidar_mount.stl]: os2_mount.stl

── Parametric Mode ──────────────────────────────────────────
Enter mounting hole positions relative to LiDAR centre (mm).

Screw diameter mm [3.0 = M3, 4.0 = M4]: 3.0
Enter each hole as  x,y  (blank line when done, need ≥ 2):
  hole 1: -41.5,-41.5
  hole 2: 41.5,-41.5
  hole 3: 41.5,41.5
  hole 4: -41.5,41.5
  hole 5:           ← 空行で入力終了
```

入力座標はLiDAR底面の中心を原点 (0, 0) とした相対座標（mm単位）で指定します。

---

### CLIオートモード（STPファイル解析）

```bash
# 基本
python mount_generator.py -i FalconK2.stp -o mount.stl

# STEP形式で出力（CADで後編集する場合）
python mount_generator.py -i FalconK2.stp -o mount.step

# プレート厚さとボス高さを変更
python mount_generator.py -i FalconK2.stp --plate-thickness 8 --boss-height 5 -o mount.stl
```

実行時のログ出力例：

```
INFO: Loading STP: FalconK2.stp
INFO: BBox  X[-65.00 – 65.00]  Y[-55.00 – 55.00]  Z[0.00 – 98.00]  (search Z ≤ 24.50)
INFO:   Hole found: xy=(+38.00, +32.00)  z=2.10  dia=3.20 mm
INFO:   Hole found: xy=(-38.00, +32.00)  z=2.10  dia=3.20 mm
INFO:   Hole found: xy=(+38.00, -32.00)  z=4.50  dia=3.20 mm
INFO:   Hole found: xy=(-38.00, -32.00)  z=4.50  dia=3.20 mm
INFO: Total holes detected: 4
INFO: Plate footprint: 96.0 × 84.0 mm  centre=(0.0, 0.0)
INFO: Saved → mount.stl
```

> **ポイント**: ログに `Hole found` が表示されれば検出成功です。  
> z 値が穴ごとに異なる場合（非平面配置）、ボス高さが自動的に調整されます。

---

### CLIパラメトリックモード（寸法を直接指定）

仕様書や図面から穴の寸法が分かっている場合に使います。

#### パターンA: 四角4穴（ピッチ指定）

```bash
# ピッチ 38.5mm の正方形4穴パターン、M3ネジ
python mount_generator.py -p --pitch 38.5 --screw-dia 3.0 -o mount.stl
```

内部的に以下の4点が生成されます:  
`(-19.25, -19.25)` `(+19.25, -19.25)` `(+19.25, +19.25)` `(-19.25, +19.25)`

#### パターンB: 任意の穴座標を指定

```bash
# 2穴（左右対称）
python mount_generator.py -p --holes "-19.25,0 19.25,0" --screw-dia 3.0 -o mount.stl

# 非対称な4穴
python mount_generator.py -p --holes "-20,-15 20,-15 20,15 -20,15" --screw-dia 4.0 -o mount.stl
```

`--holes` の書式: `"x1,y1 x2,y2 x3,y3 ..."` （スペース区切り、クォートで囲む）

#### Ouster OS2 の場合

公式仕様書PDFから穴位置の座標を確認してから実行してください。

```bash
# OS2 の底面 M3 穴座標（仕様書で要確認）
python mount_generator.py -p \
  --holes "-41.5,-41.5 41.5,-41.5 41.5,41.5 -41.5,41.5" \
  --screw-dia 3.0 \
  -o os2_mount.stl
```

> **重要**: 上記の座標は参考値です。必ず手元の仕様書PDFの寸法図で実際の値を確認し、正確な座標に置き換えてください。

---

### 全CLIオプション一覧

```
usage: mount_generator [-h] [-i STP] [-o FILE] [-p]
                       [--pitch MM] [--holes "x1,y1 x2,y2 ..."] [--screw-dia MM]
                       [--plate-thickness MM] [--plate-margin MM]
                       [--boss-height MM] [--tripod-hole-dia MM]

オプション:
  -h, --help                ヘルプを表示
  -i STP, --input STP       入力STP/STEPファイルのパス（自動検出モード）
  -o FILE, --output FILE    出力ファイルパス [デフォルト: lidar_mount.stl]
  -p, --parametric          パラメトリックモード（STP解析をスキップ）

パラメトリックオプション:
  --pitch MM                正方形4穴パターンのピッチ（mm）
  --holes "x1,y1 x2,y2"    穴座標を直接指定（クォートで囲む）
  --screw-dia MM            ネジ径（mm） [デフォルト: 3.0 = M3]

設計変更オプション:
  --plate-thickness MM      プレート厚さ（mm） [デフォルト: 10.0]
  --plate-margin MM         プレート外形の余白（mm） [デフォルト: 10.0]
  --boss-height MM          ボス（スペーサー）の最小高さ（mm） [デフォルト: 3.0]
  --tripod-hole-dia MM      三脚ネジ穴の径（mm） [デフォルト: 6.5]
```

---

## 設計パラメータ一覧

コード内の `Config` クラスで定義されているパラメータです。  
CLIオプションにないものはコードを直接編集して変更します。

```python
@dataclass
class Config:
    plate_thickness:  float = 10.0   # プレート厚さ (mm)
    plate_margin:     float = 10.0   # 穴配置からプレート端までの余白 (mm)
    plate_fillet:     float = 3.0    # 外形コーナーのフィレット半径 (mm)
    boss_height:      float = 3.0    # ボスの最小高さ / 放熱ギャップ (mm)
    boss_od_factor:   float = 2.8    # ボス外径 = ネジ径 × この係数
    boss_od_min_wall: float = 3.0    # ボスの最小肉厚 (mm)
    screw_clearance:  float = 0.4    # ネジ穴クリアランス（直径に加算） (mm)
    tripod_hole_dia:  float = 6.5    # 三脚ネジ穴径 1/4-20 UNC クリアランス (mm)
    hex_nut_af:       float = 11.5   # 六角ナットの対辺距離 AF (mm)
    hex_nut_depth:    float = 5.0    # 六角ナットポケットの深さ (mm)
    chamfer_top:      float = 0.5    # 上面エッジのチャンファー (mm)
```

---

## トラブルシューティング

### `No module named 'cadquery'` エラー

```bash
pip install cadquery
```

仮想環境が有効になっているか確認してください。

### STP解析で穴が検出されない

以下を試してください:

1. **パラメトリックモードで手動入力する**（最も確実）
   ```bash
   python mount_generator.py -p --pitch 40.0 --screw-dia 3.0 -o mount.stl
   ```

2. **検出Z範囲を広げる**（コード内 `load_and_detect_holes` の `z_ratio=0.25` を大きくする）

3. **STPファイルのモデル向きを確認する**  
   CADソフトで開いたときに底面が -Z 方向（下向き）になっているか確認してください。

### `fillet failed` / `chamfer failed` の警告

機能的な問題はありません。形状の複雑さによりフィレット/チャンファーが適用できなかった場合に表示される警告です。生成されたSTLは使用可能です。

### 生成されたSTLに三脚穴がない

三脚穴（φ6.5mm）はプレートの中央（穴パターンの重心位置）に配置されます。  
万一LiDARのネジ穴パターンの重心と重なる場合は、`pcx` / `pcy` の位置をずらすかプレートを手動で修正してください。

### Bambu Studio でインポートできない

- STL形式を選択しているか確認
- ファイルサイズが正常か確認（0バイトのファイルはエラー）
- `INFO: Saved → mount.stl` のログが表示されていることを確認

### Ouster OS2 の穴座標の確認方法

公式の Ouster Hardware User Manual PDF を参照してください:  
`https://data.ouster.io/downloads/hardware-user-manual/hardware-user-manual-rev7-os2.pdf`

PDF内の「Mechanical Interface」セクションの寸法図から以下を読み取ります:
- 穴の XY 座標（センサー中心を原点とした相対値）
- ネジ径（M3 / M4 / M6 から選択）

読み取った値を `--holes` オプションに指定して実行してください。

---

## ライセンス

このプロジェクトのライセンスについては、リポジトリのルートにある `LICENSE` ファイルを参照してください。

---

## 依存ライブラリ

| ライブラリ | バージョン | 用途 |
|---|---|---|
| [CadQuery](https://cadquery.readthedocs.io/) | ≥ 2.4.0 | STP解析・3Dモデル生成（OpenCASCADE を内包） |

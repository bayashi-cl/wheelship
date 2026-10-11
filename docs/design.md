# wheelship 仕様・設計・実装方針

Python アプリケーションを Linux / Windows / AWS Lambda 向けに配布するためのビルドツール。
PyInstaller と PEX の中間に位置づける。

---

## 1. 背景と目的

### 1.1 既存ツールの課題

| ツール | 方式 | 課題 |
|---|---|---|
| PyInstaller | import を静的解析し、モジュールを独自アーカイブ（PYZ）へ凍結（freeze） | パッケージが「インストール」されないため、`importlib.metadata`・entry points・`importlib.resources`・`__file__` 依存コードが壊れやすい。hidden import や hook の手当てが必要。`sys.executable` が Python ではないため `multiprocessing`、`subprocess [sys.executable, "-m", ...]` に特別な対応が要る |
| PEX | zip（zipapp）に wheel を詰め、実行時に `sys.path` を組み立てる | Windows 非対応（シンボリックリンク、POSIX ファイルロック、shebang、`execv` を前提にしている）。インタプリタを同梱しない |
| shiv / zipapp | PEP 441 zipapp | インタプリタを同梱しない。ネイティブ拡張を含むと展開が必要 |
| PyApp | Rust 製ランチャが初回起動時に Python を取得してパッケージをインストール | インストールが**実行時**に起きる（ネットワーク・pip が必要になり、ビルドの再現性が下がる） |

### 1.2 目指すもの

- **ビルド時に、wheel を標準どおり「本当にインストール」する。** 実行時は普通の CPython が、普通の site-packages からロードする。凍結しない。
- **Windows を最初から対象にする**（POSIX 専用の仕組みに頼らない）。
- **インタプリタ同梱 / 非同梱の両モードを持つ。** 同梱するときは python-build-standalone（PBS）を使う。
- **AWS Lambda 向けの成果物（デプロイ zip / Layer / カスタムランタイム）を直接作れる。**
- **できるだけ Python の標準（PEP・PyPA の仕様）に従う。** 独自仕様は、ランチャとバンドルのマニフェストだけにする。

### 1.3 非目標（初期）

- import 解析による不要コードの除去（tree shaking）。サイズ削減は除外パターンで行う。
- macOS 対応（設計上は拡張できるようにしておく。PBS には macOS 版もある）。
- ネイティブコードへのコンパイル（Nuitka の領域）。
- インストーラ（MSI / deb / rpm）の生成。成果物をそれらに渡せばよい。

---

## 2. 準拠する標準

| 用途 | 標準 | 使い方 |
|---|---|---|
| プロジェクト定義 | PEP 621 `pyproject.toml` `[project]` | name / version / requires-python / dependencies / scripts を取得 |
| ツール設定 | `[tool.wheelship]` | 独自設定はここだけに置く |
| アプリの wheel 化 | PEP 517 / 518 | `build` パッケージ（または `pyproject_hooks`）で wheel を作る |
| ロックファイル | **PEP 751 `pylock.toml`** | 入力の主形式。uv プロジェクトはビルド時に `uv.lock` から `uv export` で生成する。それ以外は `pip lock` などで作った `pylock.toml` を置く |
| 解決レポート（代替） | pip installation report（JSON） | ロックが無い場合、`pip install --dry-run --report` をターゲット指定付きで実行して解決する |
| 互換性判定 | wheel のタグ（PEP 425 / 600 manylinux / 656 musllinux）、PEP 508 マーカー | `packaging.tags` と `packaging.markers` を使い、**ターゲット環境**に対して評価する（ホストではない） |
| インストール | Binary distribution format（PEP 427）、RECORD | `installer` ライブラリで sysconfig スキームへ展開する |
| エントリーポイント | Entry points 仕様（`console_scripts` / `gui_scripts`） | ランチャの生成元にする |
| バイトコード | PEP 552（hash ベースの pyc） | `unchecked-hash` でコンパイルして再現性を確保。読み取り専用の FS（Lambda）での起動も速くなる |
| zipapp | PEP 441 | インタプリタ非同梱の単一ファイル形式 |
| SBOM（任意） | PEP 770（`.dist-info/sboms`）、CycloneDX | ロック内容から生成する |
| 再現性 | `SOURCE_DATE_EPOCH` | アーカイブ内のタイムスタンプとエントリ順を固定する |

設計原則: **「pip でそのインタプリタにインストールした状態」と同じものを、ビルド時に、ターゲット向けに作る。** そのため成果物の中で `importlib.metadata.distributions()` を使えば、普通の環境と同じ結果になる（pip は同梱しないが、`RECORD` などのメタデータは標準どおりに置く）。

---

## 3. 用語とモデル

- **Target**: 配布先の環境。`os`（linux / windows）、`arch`（x86_64 / aarch64）、`libc`（glibc のバージョン。musl は対象外）、`python`（例: 3.12）、`implementation`（cpython、将来は free-threaded の `cp313t` も）の組で決まる。
  - ここから `packaging.tags` の互換タグ列と PEP 508 のマーカー環境（`sys_platform`、`platform_machine`、`python_full_version` など）を**合成**する。ホストの値は使わないので、Linux 上で Windows 向けにクロスビルドできる。
- **Interpreter mode**: `bundled`（PBS を同梱）/ `system`（配布先の Python を使う）。
- **Bundle**: インストール済みのディレクトリツリー＋マニフェスト `wheelship.json`。すべての出力形式はこれを中間表現にする。
- **Format**: Bundle の包み方（dir / archive / exe / pyz / lambda-*）。

```
pyproject.toml ──► [wheel build] ──► app.whl ─┐
pylock.toml ────► [select wheels for Target] ─┼─► [install via installer] ─► Bundle(dir) ─► Format
PBS release ────► [fetch + verify] ───────────┘        (+ pyc compile, prune, launcher)
```

---

## 4. 出力形式

| Format | Interpreter | Linux | Windows | Lambda | 概要 |
|---|---|:-:|:-:|:-:|---|
| `dir` | bundled / system | ✓ | ✓ | – | 展開済みディレクトリ（onedir 相当） |
| `archive` | bundled / system | ✓ (tar.gz) | ✓ (zip) | – | `dir` をアーカイブしたもの |
| `exe` | bundled / system | ✓ | ✓ | – | 単一実行ファイル。ネイティブランチャの後ろに zip を連結し、初回起動時にキャッシュへ展開する |
| `pyz` | system | ✓ | ✓ | – | PEP 441 zipapp。ネイティブ拡張は初回にキャッシュへ展開する（shiv 方式） |
| `lambda-zip` | system（マネージドランタイム） | – | – | ✓ | `/var/task` 直下に配置するデプロイパッケージ |
| `lambda-layer` | system | – | – | ✓ | `python/` 配下に依存を置いた Layer |
| `lambda-runtime` | bundled（`provided.al2023`） | – | – | ✓ | `bootstrap` ＋ PBS ＋ `awslambdaric` を入れたカスタムランタイム |

### 4.1 `dir` のレイアウト（bundled）

```
myapp/
├── myapp(.exe)          # ランチャ（console_scripts ごとに 1 つ。gui_scripts は Windows GUI サブシステム版）
├── wheelship.json           # マニフェスト
└── python/              # PBS の install_only(_stripped) ツリー
    ├── bin/python3.12   # Linux（dir / tar.gz では PBS の相対 symlink を保持。zip 系では実体化）
    ├── python.exe       # Windows
    └── lib/python3.12/site-packages/   # ← アプリと依存をここへインストール（Windows では Lib/site-packages）
```

- アプリと依存は、**同梱インタプリタ自身の sysconfig スキーム**（purelib / platlib / scripts / data）にインストールする。pip がそのインタプリタにインストールするのと同じ場所で、最も標準に近い。
- 起動時は `python -I`（isolated mode）を既定にする。ユーザーの site-packages、`PYTHON*` 環境変数、カレントディレクトリが混ざらない。`-I` を外す設定も用意する。
- `sys.executable` は本物の `python` を指す。そのため `multiprocessing`（Windows の spawn を含む）や `subprocess.run([sys.executable, "-m", ...])` は何もしなくても動く。PyInstaller に対する大きな利点。

### 4.2 `dir` のレイアウト（system）

```
myapp/
├── myapp(.exe)            # ランチャ（下記の順でインタプリタを探す）
├── wheelship.json
├── _wheelship_boot.py     # ブートストラップ
└── lib/                   # purelib = platlib = lib/ のスキームでインストールした site ディレクトリ
```

- ランチャがインタプリタを探す順序: `WHEELSHIP_PYTHON` 環境変数 → マニフェストの候補を PATH から探す。候補は Linux が `python3.12` → `python3`、Windows が `py -3.12`（PEP 397 の py ランチャ。GUI 用は `pyw`）→ `python`。
- 起動コマンドは `<python> -I -S <root>/_wheelship_boot.py <entry point 名> <ランチャのパス> <ユーザー引数>`。`-S` により、使う Python 自身の site-packages（システムや venv）は `sys.path` に入らない。マニフェストの `{root}/` で始まる引数は、ランチャがバンドルの場所に置き換える。
- `_wheelship_boot.py` の処理（標準ライブラリのみ。古い Python でもバージョン不一致を報告できるよう、新しい構文は使わない）:
  1. ABI の確認: `sys.implementation.name`、メジャー・マイナーバージョン、`sysconfig.get_config_var("EXT_SUFFIX")`（アーキテクチャや glibc / musl の違いも表れる）をマニフェストと照合する。合わなければ、何が必要かと `WHEELSHIP_PYTHON` の指定方法を表示して終了コード 1 で終わる。
  2. `site.addsitedir(lib)` を呼ぶ。`.pth` も処理されるので、名前空間パッケージや `.pth` に依存するパッケージも動く。
  3. エントリーポイント（`module:attr`）を import して呼ぶ。`sys.argv[0]` はランチャのパスにする。
- 依存パッケージの console script（例: `pygmentize`）は生成しない。インタプリタを同梱しないので、指す先を確実に決められないため。

### 4.3 `exe`（単一ファイル）

```
[ ランチャ ][ payload.zip ][ meta.json ][ "WHSHPAY1" ][ payload offset u64 ][ payload 長 u64 ][ meta 長 u64 ]
                                          [ （Windows: 署名時に付く Authenticode 証明書テーブル） ]
```

- bundled / system のどちらのツリーも、そのまま payload にできる（ランチャは中身を区別しない）。
- payload はツリー全体の zip。Linux の symlink は symlink のまま格納する（`python3 -> python3.12` を実体化すると 30MB 増えるため）。ツリー直下のランチャは外側の exe と重複するので入れない。
- 起動の流れ:
  1. 自身の末尾 1MiB から目印を後方探索する。署名で証明書テーブルが末尾に付いても見つかる。目印はランチャのバイナリ自身に定数として現れないよう、反転した値で持って実行時に戻す。検証に通らない候補は読み飛ばす。
  2. キャッシュ `<cache>/apps/<アプリ名>/<payload の sha256 先頭16桁>/` に `wheelship.json` があれば、そのまま使う。
  3. 無ければ同じ階層の `.tmp-<pid>-<時刻>` に展開してから rename する。並行起動で rename が競合したら一時ディレクトリを捨て、先に完成したものを使う。**ロックファイルは使わない**ので、Windows でも安全。zip のパスが外に出ないことと、symlink がバンドルの外を指さないことを確認する。
  4. 展開先をバンドルの場所として、通常のランチャと同じ手順で起動する。`sys.argv[0]` は外側の exe のパスになる。
- キャッシュディレクトリ: Linux は `$XDG_CACHE_HOME/wheelship/apps`（無ければ `~/.cache/wheelship/apps`）、Windows は `%LOCALAPPDATA%\wheelship\cache\apps`。`WHEELSHIP_CACHE_DIR` を指定するとその下の `apps/` を使う（ビルド用キャッシュと同じ場所）。
- 一度展開すれば 2 回目以降は展開しない。参考（76MB のバンドル）: 初回 0.6 秒、2 回目以降 0.12 秒。
- 古いバージョンは、各バージョンの最終利用時刻（`.wheelship-last-used`）を見て、現在のもの以外に新しい 2 つまで残して削除する。1 時間以上前の一時ディレクトリも削除する。失敗しても無視する（Windows で実行中のものは消せないため）。
- 出力名は Windows が `<名前>-<版>-<ターゲット>.exe`、Linux が `<名前>-<版>-<ターゲット>.bin`（dir 形式の出力ディレクトリと名前が重ならないように）。entry point はファイル名で選ぶので、複数ある場合はコピーやリネームで使い分ける。名前が一致しなければ既定の entry point を使う。

### 4.3.1 `pyz`（system、PEP 441 zipapp）

- 中身は `__main__.py`（ブートストラップと同じファイル）、`wheelship.json`、`lib/`。先頭に `#!/usr/bin/env python3.12` を付ける。Windows では py ランチャがこの shebang を解釈する。
- ネイティブ拡張は zip から import できないため、初回に `lib/` をキャッシュへ展開する。展開先や手順は exe と同じ（キー: `lib/` の内容の sha256）。
- shebang からは `-I -S` を渡せないため、ブートストラップが Python 自身の site-packages を `sys.path` から取り除く。ユーザー環境変数（`PYTHONPATH` など）の影響は受ける。

### 4.4 Lambda

| Format | 配置 | 備考 |
|---|---|---|
| `lambda-zip` | zip 直下に依存＋アプリ（`/var/task`） | handler は設定 `module:func` を Lambda 形式 `module.func` に変換して表示する |
| `lambda-layer` | `python/` 配下 | `/opt/python` が `sys.path` に入る |
| `lambda-runtime` | `bootstrap`、`python/`（PBS）、site-packages に app と `awslambdaric` | `provided.al2023` 用のカスタムランタイム。`bootstrap` は `exec "$(dirname "$0")/python/bin/python3.X" -I -m awslambdaric "$_HANDLER"`。自分の場所を基準にするので、関数パッケージ（`/var/task`）としてもレイヤー（`/opt`）としても動く。AWS がマネージドランタイムを提供していない Python バージョンも動かせる |

- ターゲットの既定値:
  - python3.12 以降のマネージドランタイムは AL2023（glibc 2.34）なので、`manylinux_2_34` 以下の wheel を受け入れる。python3.11 以前は AL2（glibc 2.26）なので `manylinux_2_26` 以下。
  - アーキテクチャは `x86_64` / `arm64`（= aarch64）。
- `/var/task` は読み取り専用で、実行時に pyc を書けない。そのため**ビルド時に pyc を事前コンパイルする**（コールドスタート対策）。コンパイルにはターゲットと同じマイナーバージョンのインタプリタが必要なので、ホスト用の PBS を自動で取得して使う（pyc はプラットフォームに依存しない）。
- サイズ上限（zip 50MB 直接アップロード / 展開後 250MB）を超えそうなら警告する。`--prune` で tests / `__pycache__` / `*.pyi` などを除く。`*.dist-info` は `importlib.metadata` が使うので**消さない**。
- zip のエントリには Unix パーミッション（`external_attr`）を明示的に設定する。Windows 上でビルドしても `bootstrap` や `python3` が実行可能になる。
- `lambda-runtime` の詳細:
  - `awslambdaric`（AWS 公式の Runtime Interface Client）は、アプリの依存として pylock に入れてもらう（独自に同梱しない。ロックで版が固定され、SBOM にも載る）。無ければエラーにする。
  - ターゲットは `lambda-*` プラットフォームで `formats = ["lambda-runtime"]` を指定する。インタプリタは自動で `bundled` になり、glibc は Python のバージョンに関係なく `provided.al2023` の 2.34 になる。
  - 画面が無いので、`tkinter` を削除グループに自動で加える。zip では symlink が実体化されるため、`python/bin/python`・`python3` の alias は削除する（`bootstrap` は `python3.X` を直接呼ぶ）。
  - 参考サイズ（3.14、x86_64）: zip 24MB、展開後 63MB。
- 検証: AWS 公式のベースイメージ（`public.ecr.aws/lambda/python:3.12`、`public.ecr.aws/lambda/provided:al2023`）に同梱の Runtime Interface Emulator で、成果物を読み取り専用の `/var/task` に置いて実際に呼び出すテストを持つ（docker が使える環境のみ）。

### 4.5 SBOM

- ターゲットごとに CycloneDX 1.6 の JSON を成果物の隣に出力する（`<名前>-<版>-<ターゲット>.cdx.json`）。`[tool.wheelship].sbom = false` で無効にできる。
- 内容: アプリ本体、インストールした各ディストリビューション（purl `pkg:pypi/...`、インストールした wheel の sha256、配布 URL、ライセンス）、同梱した PBS（版、アセットの sha256、ダウンロード URL）、成果物に入れたランチャ（sha256）。
- ライセンスはコアメタデータから取る。`License-Expression`（PEP 639）があればそれを、無ければ Trove 分類子の名前を、それも無ければ `License` 欄（短いものだけ）を使う。分類子はバージョンが特定できないため SPDX ID には変換しない。
- 決定的に出力する: タイムスタンプは `SOURCE_DATE_EPOCH`、`serialNumber` は内容のハッシュから作る UUIDv5。

---

## 5. python-build-standalone の扱い

- 取得元: `astral-sh/python-build-standalone` の GitHub Releases。
- 設定でリリースタグ（日付）を**固定**できる。省略したら最新（`latest-release.json`）を解決し、そのタグとアセットの sha256 を `wheelship.lock` に記録する（`pylock.toml` は PEP 751 のスキーマを汚さないよう、触らない）。2 回目以降は `wheelship.lock` だけで決まるため、ネットワークでメタデータを引かない。

  ```toml
  # wheelship.lock（コミットする）
  version = 1
  [pbs]
  release = "20261009"
  [[pbs.asset]]
  name = "cpython-3.12.15+20261009-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
  sha256 = "..."
  ```
- 社内ミラーは `WHEELSHIP_PBS_MIRROR`（`<mirror>/<release>/<asset>` の形）で指定する。
- アセット名は `cpython-{version}+{tag}-{triple}-{flavor}.tar.gz`。
  - triple: `x86_64-unknown-linux-gnu`、`aarch64-unknown-linux-gnu`、`x86_64-pc-windows-msvc`、`aarch64-pc-windows-msvc`（musl は対象外）。
  - flavor: 既定は `install_only_stripped`（デバッグシンボルなし）。`install_only` も選べる。`full`（`.tar.zst`）は使わない。標準ライブラリだけで扱えないため。Python 3.14 以降なら `compression.zstd` で扱える。
- 初回はリリースの `SHA256SUMS`、以降は `wheelship.lock` の sha256 で検証する。ダウンロードは内容アドレスのキャッシュに置く。free-threaded 版とプレリリースは選ばない。
- 後処理:
  - Linux のシンボリックリンク（`python3 -> python3.12` など）は、dir と tar.gz ではそのまま保持する。zip 系の形式（exe の payload など）では実体化する。
  - `pip`・`ensurepip` と、ビルドマシンの shebang が入った PBS 付属スクリプト（`2to3`・`idle`・`pydoc`）は**常に削除する**（pip は同梱しない）。
  - `[tool.wheelship.pbs].strip` のグループ単位で削除する。既定は `["dev", "test", "idlelib"]`。
    - `dev`: ヘッダ、`libpython*.so`（約 32MB。Linux の `python3.X` は静的リンクなので実行には不要）、`*.a`、`config-*`、pkgconfig、man、Windows の `libs/`
    - `test`: 標準ライブラリのテスト
    - `idlelib`: IDLE
    - `tkinter`: tkinter と Tcl/Tk（約 9MB。GUI に tkinter を使うアプリでは外さない）
  - PBS の pyc はタイムスタンプ方式で、アーカイブ時に mtime を正規化すると無効になる。そのため、ビルド時に標準ライブラリも含めて `unchecked-hash` で作り直す（`compileall -f`）。ターゲットがホストで動くなら同梱インタプリタ自身で、そうでなければホスト用の PBS を取得してコンパイルする。
  - 参考サイズ（3.12、x86_64 Linux、既定の strip）: 展開後約 76MB（インタプリタ 30MB、標準ライブラリ 37MB、Tcl/Tk 9MB）、tar.gz で約 26MB。
- ライセンス:
  - CPython の `LICENSE.txt` は削除対象にしない。
  - PBS がリンクしている OpenSSL・SQLite・libffi などのライセンス文は `install_only` 版の配布物に含まれない（完全版アーカイブにだけある）。現状は SBOM に PBS の版と sha256 を記録するところまでで、ライセンス文の同梱は今後の課題。
  - 依存パッケージのライセンスは、wheel の `.dist-info/licenses/`（PEP 639）がそのまま成果物に入る。`prune` でライセンスファイルを消すと警告する。
  - ランチャ（Rust）が使っている crate のライセンス表記の生成は今後の課題。

---

## 6. 依存解決とインストール

### 6.1 解決

1. **`pylock.toml`（推奨）**: `uv export --format pylock.toml` で生成した**ユニバーサル**ロックには、全プラットフォーム分の wheel がマーカー付きで入っている。1 つのロックから複数の Target をビルドできる。
   - **uv プロジェクトでは `uv.lock` だけをコミットする。** `pylock.toml` は `uv.lock` から決定的に作れるので、両方をコミットしても再現性は増えず、再生成し忘れて食い違うリスクが増えるだけ。ロックの探索順は次の通り。
     1. `--lock` / `[tool.wheelship].lock`
     2. `pyproject.toml` と同じディレクトリの `pylock.toml`
     3. プロジェクトのディレクトリから親へたどって最初に見つかる `uv.lock`（workspace のルート）
     4. どれも無ければ代替（下記の 2.）
   - `uv.lock` のときは、プロジェクトのディレクトリで `uv export --format pylock.toml --quiet --no-default-groups [--group G] [--extra E] --locked <uv-export-args>` を実行し、標準出力を作業ディレクトリに書いて読む。
     - workspace のメンバーは、カレントディレクトリから uv が自動で選ぶ。`--package` は付けない。
     - `--locked` を付けるので `uv.lock` は書き換えない。`pyproject.toml` とずれていればエラーにする。`uv-export-args` に `--frozen` があるときは `--locked` を付けない。
   - uv はエクスポートに extras / groups をマーカーとしては書かず、選んだ結果だけを出力する。そのため `[tool.wheelship]` の `extras` / `groups` は uv の引数に変換する。`groups` を省略したときは `--no-default-groups` になり、dev などの default groups は入らない。
   - エクスポート中のパスは workspace のルート（`uv.lock` のあるディレクトリ）からの相対パスになる。`-o` で出力先を変えても同じ。そのため、パスは `uv.lock` のディレクトリを基準に解決する。
   - uv は workspace を自分で決めるので、別の `uv.lock` を明示するとパスの基準がずれる。アプリ自身の directory エントリが `pyproject.toml` のディレクトリを指していなければエラーにする。
   - エクスポートの方法を完全に自分で決めたいとき（uv を使わない、独自のパイプラインがある）は、自分で `pylock.toml` を作って 1. で指定する。任意のシェルコマンドを設定で受け付けることはしない。出力先、パスの基準、extras/groups との整合を wheelship が保証できなくなるため。
2. **代替**: ロックが無い場合、Target ごとに次を実行してレポートを入力にする。
   `pip install --dry-run --ignore-installed --report - --only-binary=:all: --platform <tags> --python-version <v> --implementation cp --abi <abi> -r ...`
   この場合、ロックは使わないので再現性は保証しないと明示する。

wheelship 自身はリゾルバを持たない。解決は標準ツール（uv / pip）に任せる。

### 6.2 wheel の選択

- 各パッケージについて、PEP 508 マーカーを**Target 環境**で評価する → 候補の wheel を `packaging.tags` の優先順位で選ぶ → ハッシュ検証付きでダウンロードする（キャッシュあり）。
- sdist しか無い場合:
  - ホストと Target が一致していれば、PEP 517 で wheel をビルドする。
  - クロスビルドならエラーにする。`--find-links` で事前ビルド済みの wheel を指定してもらう。

### 6.3 インストール

- `installer` ライブラリで、Target のスキームへ展開する。
  - bundled: PBS の sysconfig パスを、ターゲット OS 用にパスを計算して使う（Windows は `Lib/site-packages`、Linux は `lib/python3.X/site-packages`）。
  - system: `purelib = platlib = lib/`、`scripts = bin/`、`data = .`。
- `RECORD`、`INSTALLER`（=`wheelship <version>`）を書く。`direct_url.json`（PEP 610）は、ビルドしたホストの絶対パスが入って再現性を損なうため書かない。
- **console script は installer の生成物を使わない。** 絶対パスの shebang が入って再配置できないため、次のように置き換える。
  - アプリ自身の `console_scripts` / `gui_scripts`: バンドル直下に wheelship ランチャを置く。
  - 依存パッケージのスクリプト（例: `pygmentize`）: `python/bin` / `python/Scripts` に、相対パスでインタプリタを探すスクリプトを書く。Linux は `/bin/sh` と Python の polyglot（`'''exec' "$(dirname -- "$(realpath -- "$0")")/python3.X" -I "$0" "$@"`）、Windows は `%~dp0..\python.exe` を呼ぶ `.cmd`。
- アプリ本体も wheel 化してから同じ経路でインストールする（「そのままインストールする」原則）。

---

## 7. ランチャ

- **Rust 製**の小さなバイナリ。std と最小限の crate（zip 展開など）だけを使い、数百 KB を目標にする。
- 事前ビルドしたもの（linux-gnu x86_64/aarch64、windows x86_64/aarch64、console/gui）を**wheelship の wheel に同梱する**。distlib が `t64.exe` などを同梱しているのと同じ方式。こうすると wheelship 本体は pure Python のままで、どのホストからでもクロスビルドできる。
- 設定は実行ファイルに直接埋め込まない。隣の `wheelship.json`（exe 形式では payload 内）を読む。ランチャ名（実行ファイルの stem）で entry point を選ぶ、busybox 方式。名前が一致しない（リネームされた）場合は `default_entry_point` を使う。
- 実行するコマンドは `<interpreter> <python-flags> -c "import sys; sys.argv.pop(0); from mod import f; sys.exit(f())" <ランチャのパス> <ユーザー引数>`。`-c` のプレースホルダを取り除くので、`sys.argv[0]` はランチャのパスになり、argparse の `prog` も通常の console script と同じになる。コードは wheelship が生成してマニフェストに書くので、ランチャは中身を解釈しない。
- ランチャ自体へのシンボリックリンク（`~/.local/bin` に置くなど）は、実体の場所まで解決してからバンドルを探す。
- 起動に失敗した場合は `wheelship-launcher: ...` を stderr に出して終了コード 1 で終わる。Windows の GUI 版では、コンソールが無いのでメッセージボックスで表示する。
- プロセスモデル:
  - Linux: `execv` で python に置き換わる。PID とシグナルの扱いが自然になる。
  - Windows: 子プロセスとして起動して、その終了コードを返す。Ctrl-C / Ctrl-Break は親では無視し、コンソールを共有する子に任せる。子は Job Object（`KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK`）に入れ、ランチャが強制終了されたら Python も終わるようにする。SILENT_BREAKAWAY_OK により、Python がさらに起動したプロセスは Job に入らないので、アプリが常駐プロセスを起動しても巻き込まれない。
  - Windows で `python3XX.dll` をロードして `Py_Main` を呼ぶ 1 プロセス方式は、§7.1 で検討した。現時点では実験的なオプトイン（`WHEELSHIP_EMBED=1`）にとどめる。
- デバッグ用の環境変数（アプリの引数と衝突しないよう、引数ではなく環境変数で受け取る）:
  - `WHEELSHIP_DEBUG=1`: 探索・展開の様子をログ出力する。
  - `WHEELSHIP_BOOT=python`: entry point ではなく同梱 Python の REPL / 引数をそのまま実行する。
  - `WHEELSHIP_CACHE_DIR`: キャッシュディレクトリを上書きする。
  - `WHEELSHIP_EMBED=1`: Windows の bundled で、Python をランチャのプロセス内で動かす（実験的、§7.1）。
- Windows の長いパス対策: キャッシュパスを短く保ち、展開時は `\\?\` プレフィックスを使う。
- Windows のランチャは C ランタイムを静的リンクする（`.cargo/config.toml` の `+crt-static`）。Visual C++ 再頒布可能パッケージが無い環境でも起動するようにするため。PBS は `vcruntime140.dll` を `python/` に同梱しているが、ランチャの DLL 検索パスには入らない。

### 7.1 `Py_Main` 埋め込み方式の検討（Phase 4）

**方式**: Windows の bundled で、ランチャが `python/python3XX.dll` を `LoadLibraryExW(LOAD_WITH_ALTERED_SEARCH_PATH)` でロードし、`Py_Main(argc, argv)` を呼ぶ。子プロセスは作らない。
- `Py_Main` のシグネチャは Python 3.0 から変わっていない。ヘッダも、バージョンごとの構造体レイアウトも不要。
- DLL の場所はマニフェストの `interpreter.library` に書く。PBS の Windows 版にだけ出力する。
- `sys.prefix` は、CPython の getpath が DLL の隣の `Lib/os.py` から `python/` と判定する。`PYTHONHOME` などの設定は要らない。

**`sys.executable` の扱い**: そのままだと `sys.executable` がランチャ（`app.exe`）になる。`multiprocessing`（spawn）や `subprocess.run([sys.executable, ...])` が、Python ではなくアプリ自身を起動してしまう。
- 対策として、ランチャは `-X wheelship.executable=<python.exe>` を渡す。`-c` のコードの先頭には `sys.executable = sys._base_executable = sys._xoptions.pop(...)` を足す。
- `pop` するので、`multiprocessing` が子に引き継ぐ `-X` オプション（`_args_from_interpreter_flags`）にも残らない。
- GUI の entry point では `pythonw.exe` を設定する。

**検証**（`tests/integration/test_embed.py`、Windows CI。2026-10-11 時点）:
- **動作**: 次がすべて spawn 方式と同じ結果になった。
  - `os.getpid()` がランチャの PID と一致する（1 プロセスで動いている）。
  - `sys.executable`、`sys.prefix`、`sys.argv[0]`、`-I`、ネイティブ拡張、`multiprocessing`（spawn）、終了コード。
  - GUI 版（`pythonw.exe` 相当で stdout なし）。
  - 単一ファイル `exe`（キャッシュに展開して埋め込む）。
- **起動時間**（GitHub の Windows ランナー、サンプルアプリ、15 回の中央値）:

  | 方式 | 中央値 | 最小 |
  |---|---|---|
  | spawn | 108.0 ms | 106.1 ms |
  | embed | 102.9 ms | 101.6 ms |

- **pyvenv.cfg の誤検出**: getpath は、プロセスの実行ファイルと同じディレクトリか、その 1 つ上にある `pyvenv.cfg` を venv の印として読む。`-I` を付けても読む。

  | `pyvenv.cfg` の場所 | spawn | embed |
  |---|---|---|
  | ランチャの隣（= `python.exe` の 1 つ上） | 失敗 | 失敗 |
  | ランチャの 1 つ上 | 成功 | 失敗 |

  - spawn 方式では、ランチャの隣に `pyvenv.cfg` を置かれたときだけ影響を受ける。バンドルの中に置かれることになるので、現実にはまず起きない。
  - embed 方式では、判定の基準がランチャ（`app.exe`）の場所になる。特に単一ファイル `exe` は利用者が置き場所を決めるので、venv の `Scripts\` にコピーするなど、普通の使い方で壊れる。

**評価**:
- 利点:
  - タスクマネージャ上のプロセス名と PID がアプリのものになる。
  - Job Object と Ctrl-C の中継が要らない。
  - プロセス単位の設定（ファイアウォール規則、タスクバーのグループ化、アイコン）がアプリの exe に付く。
  - 起動時間は約 5 ms（5%）速い。
- 欠点:
  - `sys.executable` を後から書き換える回避策が要る。
  - `pyvenv.cfg` の誤検出が起きる範囲が広がる。
  - 拡張モジュールが読み込む DLL がランチャのプロセスに入る。

**結論**:
- 既定は spawn 方式のままとし、embed は実験的なオプトインとして残す。起動時間の差が小さく、`pyvenv.cfg` の問題は `Py_Main` では避けられないため。
- Python 3.14 以降の PEP 741（`PyInitConfig_SetStr(config, "executable", ...)` と `Py_InitializeFromInitConfig`）なら、文字列キーで `executable` を直接設定できる。構造体レイアウトに依存しないので、上の 2 つの回避策がどちらも不要になる見込みがある。既定にするかどうかは、それを確かめてから再検討する。
- Linux は `exec` ですでに 1 プロセスなので、埋め込みは行わない。
- 未確認のこと: 実コンソールでの Ctrl-C / Ctrl-Break の挙動（CI では試せない）。

---

## 8. 設定（`pyproject.toml`）

```toml
[project]
name = "myapp"
version = "1.2.0"
requires-python = ">=3.11"
dependencies = ["httpx", "pydantic"]

[project.scripts]
myapp = "myapp.cli:main"

[tool.wheelship]
# lock = "pylock.toml"          # 省略時: ./pylock.toml → 親をたどって uv.lock（ビルド時に uv export）
# uv-export-args = []           # uv.lock のときに uv export へ追加する引数
python = "3.12"                  # requires-python の範囲内であることを検証する
interpreter = "bundled"          # bundled | system
python-flags = ["-I"]
compile-bytecode = true
prune = ["**/tests/**", "**/*.pyi"]

[tool.wheelship.pbs]
release = "20251014"
flavor = "install_only_stripped"
strip = ["dev", "test", "idlelib"]   # 既定値。pip / ensurepip は常に削除

[[tool.wheelship.target]]
name = "linux"
platform = "linux-x86_64"
glibc = "2.28"                   # 受け入れる manylinux の上限
formats = ["exe", "archive"]

[[tool.wheelship.target]]
name = "windows"
platform = "windows-x86_64"
formats = ["exe", "archive"]

[[tool.wheelship.target]]
name = "lambda"
platform = "lambda-arm64"        # glibc や python の既定を Lambda に合わせたプリセット
interpreter = "system"
formats = ["lambda-zip", "lambda-layer"]

[tool.wheelship.lambda]
handler = "myapp.aws:handler"
```

## 9. CLI

```
wheelship build [--target NAME ...] [--format FMT ...] [-o DIST]   # メインコマンド
wheelship python list | fetch <ver> --platform <p>                 # PBS の管理
wheelship targets                                                  # 解決後の Target（タグ・マーカー）を表示
wheelship inspect <artifact>                                       # マニフェスト・同梱パッケージ・サイズ内訳
wheelship run [--target NAME] -- <args>                            # ホスト向けにビルドしてすぐ実行
wheelship cache clean
```

---

## 10. マニフェスト `wheelship.json`

```json
{
  "schema": 1,
  "app": {"name": "myapp", "version": "1.2.0"},
  "interpreter": {"mode": "bundled", "path": "python/bin/python3.12", "version": "3.12.7",
                  "cache_tag": "cpython-312", "ext_suffix": ".cpython-312-x86_64-linux-gnu.so"},
  "python_flags": ["-I"],
  "site_dirs": [],
  "entry_points": {"myapp": {"value": "myapp.cli:main", "gui": false}},
  "target": {"platform": "linux-x86_64", "tags": ["cp312-cp312-manylinux_2_28_x86_64", "..."]},
  "payload_sha256": "...",
  "build": {"tool": "wheelship 0.1.0", "source_date_epoch": 1760000000}
}
```

ランチャとブートストラップが読む唯一の独自フォーマットなので、スキーマにバージョンを付けて互換性を管理する。

---

## 11. 実装構成

wheelship 本体は pure Python（3.11 以上、`tomllib` を使うため）。依存は `packaging`（26.1 以上。PEP 751 の `packaging.pylock` を使う）、`installer`、`build`、`truststore`。HTTP は標準ライブラリの `urllib` で済ませる。証明書の検証には `truststore` で OS のトラストストアを使う。python-build-standalone 系のインタプリタは、OpenSSL の既定設定ではシステムの CA を見つけられない環境があるため。

### 11.1 リポジトリ構成（cargo workspace と uv workspace の併存）

リポジトリのトップに `Cargo.toml`（cargo workspace）と `pyproject.toml`（uv workspace）を並べて置く。どちらもトップは**仮想ルート**にする（root 自体はパッケージにしない）。メンバーは `crates/` と `packages/` に分ける。

```
.
├── Cargo.toml            # [workspace] 仮想マニフェスト
├── Cargo.lock
├── rust-toolchain.toml   # toolchain と targets を固定
├── pyproject.toml        # [tool.uv.workspace] 仮想ルート（[project] なし）
├── uv.lock
├── .python-version
├── crates/
│   └── wheelship-launcher/   # Rust ランチャ（bin: console / gui）
├── packages/
│   └── wheelship/            # Python 本体（配布する wheel）
│       ├── pyproject.toml
│       └── src/wheelship/...
├── tests/
│   ├── unit/ integration/
│   └── fixtures/apps/    # サンプルアプリ（それぞれ独立した pyproject。workspace からは除外）
├── scripts/              # ランチャのビルドと配置などの補助スクリプト
└── docs/
```

ルートの `Cargo.toml`:

```toml
[workspace]
resolver = "3"
members = ["crates/*"]

[workspace.package]
edition = "2024"
license = "MIT OR Apache-2.0"

[profile.release]          # ランチャを小さくする設定
opt-level = "z"
lto = true
codegen-units = 1
panic = "abort"
strip = true
```

ルートの `pyproject.toml`:

```toml
[tool.uv.workspace]
members = ["packages/*"]
exclude = ["tests/fixtures/**"]

[tool.uv.sources]
wheelship = { workspace = true }

[dependency-groups]
dev = ["wheelship", "pytest", "ruff", "mypy"]
```

**併存させるときの注意点**

- **生成物の置き場所が衝突しない**: cargo は `target/` を、uv は `.venv/` を使う。`.gitignore` には `target/`、`.venv/`、`dist/`、`packages/wheelship/src/wheelship/_launchers/` を書く。
- **ランチャを wheel に入れる方法**: maturin の `bindings = "bin"` は**ホストのプラットフォーム専用の wheel**を作るので使わない。全ターゲット分のランチャを 1 つの `py3-none-any` wheel に入れるため、次の手順にする。
  1. CI で `cargo build --release --target <triple>` を各ターゲット向けに実行する。Linux は `cargo-zigbuild` で glibc 2.17 向けに作る。Windows は Windows ランナーで作る（Linux から作るなら `cargo-xwin` を使う）。
  2. できたバイナリを `packages/wheelship/src/wheelship/_launchers/<triple>/` に配置する（git には入れない）。
  3. ビルドバックエンド（hatchling）の `artifacts` で wheel に含める。
- **開発中の参照先**: `WHEELSHIP_LAUNCHER_DIR` が指定されていればそこを使い、無ければリポジトリの `target/{debug,release}/` を探す。そのため、`cargo build` → `uv run wheelship build` の流れで、ランチャを配置し直さずに試せる。
- **サンプルアプリ**: `tests/fixtures/apps/*` はそれぞれ独立したプロジェクト（自前の `uv.lock` か `pylock.toml` を持つ。`uv_workspace` はそれ自体が uv workspace）なので、`exclude` で workspace から外す。

### 11.2 Python パッケージのモジュール構成

```
packages/wheelship/src/wheelship/
├── cli.py            # argparse
├── config.py         # pyproject 読み込み・検証・既定値
├── target.py         # Target → packaging.tags / marker 環境の合成、プリセット（lambda-* 等）
├── lock/
│   ├── __init__.py   # Artifact（選択結果の共通表現）
│   ├── pylock.py     # PEP 751: packaging.pylock.Pylock.select() に Target の環境とタグを渡す
│   ├── uv.py         # uv.lock の探索と、ビルド時の uv export（PEP 751 への変換）
│   └── pipreport.py  # pip installation report（代替経路）
├── wheels.py         # Artifact → ターゲット互換の wheel（取得 / find-links / PEP 517 ビルド）
├── fetch.py          # ダウンロード、ハッシュ検証、キャッシュ
├── pbs.py            # PBS リリース解決・取得・後処理
├── install.py        # installer ベースのインストール、スキーム計算
├── bytecode.py       # ターゲット版インタプリタでの compileall（unchecked-hash）
├── prune.py
├── bundle.py         # ビルドパイプライン（resolve → wheels → 形式ごとに install / finalize / 出力）
├── formats/
│   ├── dir.py  archive.py  exe.py  pyz.py
│   └── aws_lambda.py
├── reproducible.py   # 決定的な zip / tar 書き出し（mtime 固定、ソート、パーミッション）
├── boot/_wheelship_boot.py
└── _launchers/       # 事前ビルド済みバイナリ（CI で配置して wheel に同梱。git には入れない）
```

## 12. 実装フェーズ

| Phase | 内容 | ねらい |
|---|---|---|
| 0 ✅ | Target モデル、pylock リーダ、wheel 選択、fetch、installer によるインストール、`lambda-zip` / `lambda-layer`、決定的な zip | ランチャ不要ですぐ役立つ。クロスビルドの土台を検証する |
| 1 ✅ | PBS 取得、bundled の `dir` / `archive`、Rust ランチャ（dir モード、Linux / Windows） | PyInstaller onedir の代わりになる |
| 2 ✅ | `exe`（payload 連結、キャッシュ展開）、system モードの `dir` / `archive` / `exe` / `pyz` | 単一ファイル配布 |
| 3 ✅ | `lambda-runtime`、bytecode 事前コンパイルの自動化、prune、SBOM、gui_scripts | Lambda の最適化と仕上げ |
| 4 | Windows の Authenticode 署名手順（#1）、`Py_Main` 埋め込み方式の検討（✅ §7.1、実験的）、macOS（#2） | 拡張 |

## 13. テスト方針

- **単体テスト**: Target からのタグ・マーカー合成。golden test で、`packaging.tags` を実機で取った値と比較する。wheel 選択。スキームの計算。
- **統合テスト**（GitHub Actions の matrix: ubuntu x86_64/arm64、windows x86_64）。サンプルアプリで次を確認する。
  - ネイティブ拡張: numpy、pydantic-core、cryptography
  - `importlib.metadata` の entry points によるプラグイン探索
  - `importlib.resources` のデータファイル
  - 名前空間パッケージ、`.pth` を使うパッケージ
  - `multiprocessing`（spawn）、`subprocess [sys.executable, "-m", ...]`
  - 並行起動時のキャッシュ展開の競合、読み取り専用ディレクトリからの起動
- **クロスビルドテスト**: Linux でビルドした Windows 成果物を Windows ランナーで実行する。逆方向も行う。
- **Lambda**: `public.ecr.aws/lambda/python:3.12` / `provided:al2023` イメージと Runtime Interface Emulator でローカル実行する。
- **再現性**: 同じ入力から 2 回ビルドし、成果物のバイト一致を確認する。

## 14. 決定事項

1. **ツール名**: `wheelship`（2026-10-10 時点で PyPI・GitHub とも同名・類似名なし。wheel を標準どおりに「そのまま配る」方針から命名）。
2. **ランチャの言語**: Rust。
3. **bundled モードのインストール先**: PBS 自身の site-packages。
4. **pip**: 同梱しない（PBS から `pip` / `ensurepip` を常に削除する）。
5. **ロックが無い場合**: §6.1 のとおり、pip installation report を使う経路を許す（再現性は保証しないと警告を出す）。
6. **musl / Alpine**: 現時点では対象外。
7. **リポジトリ構成**: トップに cargo workspace と uv workspace を併存させる（§11.1）。

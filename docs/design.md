# pybox（仮称）仕様・設計・実装方針

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
| ツール設定 | `[tool.pybox]` | 独自設定はここだけに置く |
| アプリの wheel 化 | PEP 517 / 518 | `build` パッケージ（または `pyproject_hooks`）で wheel を作る |
| ロックファイル | **PEP 751 `pylock.toml`** | 入力の主形式。`uv export --format pylock.toml` / `pip lock` で生成 |
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
- **Bundle**: インストール済みのディレクトリツリー＋マニフェスト `pybox.json`。すべての出力形式はこれを中間表現にする。
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
| `exe` | bundled（system も可） | ✓ | ✓ | – | 単一実行ファイル。ネイティブランチャの後ろに zip を連結し、初回起動時にキャッシュへ展開する |
| `pyz` | system | ✓ | ✓ | – | PEP 441 zipapp。ネイティブ拡張は初回にキャッシュへ展開する（shiv 方式） |
| `lambda-zip` | system（マネージドランタイム） | – | – | ✓ | `/var/task` 直下に配置するデプロイパッケージ |
| `lambda-layer` | system | – | – | ✓ | `python/` 配下に依存を置いた Layer |
| `lambda-runtime` | bundled（`provided.al2023`） | – | – | ✓ | `bootstrap` ＋ PBS ＋ `awslambdaric` を入れたカスタムランタイム |

### 4.1 `dir` のレイアウト（bundled）

```
myapp/
├── myapp(.exe)          # ランチャ（console_scripts ごとに 1 つ。gui_scripts は Windows GUI サブシステム版）
├── pybox.json           # マニフェスト
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
├── myapp(.exe)          # ランチャ（下記の順でインタプリタを探す）
├── pybox.json
├── _pybox_boot.py       # ブートストラップ
└── lib/                 # purelib = platlib = lib/ のスキームでインストールした site-packages
```

- ランチャがインタプリタを探す順序: `PYBOX_PYTHON` 環境変数 → マニフェストに書かれた候補 → Windows では `py -3.12`（PEP 397 の py ランチャ）→ PATH 上の `python3.12` / `python3`。
- `_pybox_boot.py` の処理:
  1. ABI 互換性チェック: `sys.implementation.cache_tag` と `sysconfig.get_config_var("EXT_SUFFIX")` をマニフェストの記録と照合する。不一致なら分かりやすいエラーで終了する。
  2. `site.addsitedir(lib)` を呼ぶ。`.pth` も処理されるので、名前空間パッケージや `.pth` に依存するパッケージも動く。
  3. エントリーポイントを `importlib.metadata` で解決して呼ぶ。

### 4.3 `exe`（単一ファイル）

```
[ ランチャ stub ][ zip payload（Bundle 全体） ][ trailer: magic + payload offset + sha256 ]
                                                [ （Windows: 署名時の Authenticode 証明書テーブル） ]
```

- 起動の流れ:
  1. 自身の末尾から magic を後方探索する。署名で証明書テーブルが末尾に付いても見つかる（PyInstaller と同じ考え方）。
  2. キャッシュ `<cache>/<app>/<sha256 先頭16桁>/` に完了マーカーがあれば、そのまま使う。
  3. 無ければ `<hash>.tmp-<pid>` に展開してから rename する（アトミック）。並行起動で rename が競合したら一時ディレクトリを捨て、既存のものを使う。**ロックファイルや symlink は使わない**ので、Windows でも安全。
  4. 展開先の `python -I` で entry point を起動する。
- キャッシュディレクトリ: Linux は `$XDG_CACHE_HOME/pybox`（無ければ `~/.cache/pybox`）、Windows は `%LOCALAPPDATA%\pybox`。`PYBOX_CACHE_DIR` で上書きできる。
- 一度展開すれば 2 回目以降は展開しない。毎回 temp に展開する PyInstaller onefile より速く、AV に誤検知されにくい。
- 古いバージョンのキャッシュは、同じアプリの最新 N 個を残して削除する（ランチャが起動時に掃除する。失敗しても無視する）。

### 4.4 Lambda

| Format | 配置 | 備考 |
|---|---|---|
| `lambda-zip` | zip 直下に依存＋アプリ（`/var/task`） | handler は設定 `module:func` を Lambda 形式 `module.func` に変換して表示する |
| `lambda-layer` | `python/` 配下 | `/opt/python` が `sys.path` に入る |
| `lambda-runtime` | `bootstrap`、`python/`（PBS）、site-packages に `awslambdaric` | `bootstrap` は `exec "$LAMBDA_TASK_ROOT/python/bin/python3" -I -m awslambdaric "$_HANDLER"` 程度。AWS がまだ提供していない Python バージョン（新バージョン、free-threaded など）も動かせる |

- ターゲットの既定値:
  - python3.12 以降のマネージドランタイムは AL2023（glibc 2.34）なので、`manylinux_2_34` 以下の wheel を受け入れる。python3.11 以前は AL2（glibc 2.26）なので `manylinux_2_26` 以下。
  - アーキテクチャは `x86_64` / `arm64`（= aarch64）。
- `/var/task` は読み取り専用で、実行時に pyc を書けない。そのため**ビルド時に pyc を事前コンパイルする**（コールドスタート対策）。コンパイルにはターゲットと同じマイナーバージョンのインタプリタが必要なので、ホスト用の PBS を自動で取得して使う（pyc はプラットフォームに依存しない）。
- サイズ上限（zip 50MB 直接アップロード / 展開後 250MB）を超えそうなら警告する。`--prune` で tests / `__pycache__` / `*.pyi` などを除く。`*.dist-info` は `importlib.metadata` が使うので**消さない**。
- zip のエントリには Unix パーミッション（`external_attr`）を明示的に設定する。Windows 上でビルドしても `bootstrap` や `python3` が実行可能になる。

---

## 5. python-build-standalone の扱い

- 取得元: `astral-sh/python-build-standalone` の GitHub Releases。
- 設定でリリースタグ（日付）を**固定**できる。省略したら最新（`latest-release.json`）を解決し、そのタグとアセットの sha256 を `pybox.lock` に記録する（`pylock.toml` は PEP 751 のスキーマを汚さないよう、触らない）。2 回目以降は `pybox.lock` だけで決まるため、ネットワークでメタデータを引かない。

  ```toml
  # pybox.lock（コミットする）
  version = 1
  [pbs]
  release = "20261009"
  [[pbs.asset]]
  name = "cpython-3.12.15+20261009-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
  sha256 = "..."
  ```
- 社内ミラーは `PYBOX_PBS_MIRROR`（`<mirror>/<release>/<asset>` の形）で指定する。
- アセット名は `cpython-{version}+{tag}-{triple}-{flavor}.tar.gz`。
  - triple: `x86_64-unknown-linux-gnu`、`aarch64-unknown-linux-gnu`、`x86_64-pc-windows-msvc`、`aarch64-pc-windows-msvc`（musl は対象外）。
  - flavor: 既定は `install_only_stripped`（デバッグシンボルなし）。`install_only` も選べる。`full`（`.tar.zst`）は使わない。標準ライブラリだけで扱えないため。Python 3.14 以降なら `compression.zstd` で扱える。
- 初回はリリースの `SHA256SUMS`、以降は `pybox.lock` の sha256 で検証する。ダウンロードは内容アドレスのキャッシュに置く。free-threaded 版とプレリリースは選ばない。
- 後処理:
  - Linux のシンボリックリンク（`python3 -> python3.12` など）は、dir と tar.gz ではそのまま保持する。zip 系の形式（exe の payload など）では実体化する。
  - `pip`・`ensurepip` と、ビルドマシンの shebang が入った PBS 付属スクリプト（`2to3`・`idle`・`pydoc`）は**常に削除する**（pip は同梱しない）。
  - `[tool.pybox.pbs].strip` のグループ単位で削除する。既定は `["dev", "test", "idlelib"]`。
    - `dev`: ヘッダ、`libpython*.so`（約 32MB。Linux の `python3.X` は静的リンクなので実行には不要）、`*.a`、`config-*`、pkgconfig、man、Windows の `libs/`
    - `test`: 標準ライブラリのテスト
    - `idlelib`: IDLE
    - `tkinter`: tkinter と Tcl/Tk（約 9MB。GUI に tkinter を使うアプリでは外さない）
  - PBS の pyc はタイムスタンプ方式で、アーカイブ時に mtime を正規化すると無効になる。そのため、ビルド時に標準ライブラリも含めて `unchecked-hash` で作り直す（`compileall -f`）。ターゲットがホストで動くなら同梱インタプリタ自身で、そうでなければホスト用の PBS を取得してコンパイルする。
  - 参考サイズ（3.12、x86_64 Linux、既定の strip）: 展開後約 76MB（インタプリタ 30MB、標準ライブラリ 37MB、Tcl/Tk 9MB）、tar.gz で約 26MB。
- ライセンス: PBS とその依存（OpenSSL など）のライセンス表記を `licenses/` に同梱する。

---

## 6. 依存解決とインストール

### 6.1 解決

1. **`pylock.toml`（推奨）**: `uv export --format pylock.toml` で生成した**ユニバーサル**ロックには、全プラットフォーム分の wheel がマーカー付きで入っている。1 つのロックから複数の Target をビルドできる。
2. **代替**: ロックが無い場合、Target ごとに次を実行してレポートを入力にする。
   `pip install --dry-run --ignore-installed --report - --only-binary=:all: --platform <tags> --python-version <v> --implementation cp --abi <abi> -r ...`
   この場合、ロックは使わないので再現性は保証しないと明示する。

pybox 自身はリゾルバを持たない。解決は標準ツール（uv / pip）に任せる。

### 6.2 wheel の選択

- 各パッケージについて、PEP 508 マーカーを**Target 環境**で評価する → 候補の wheel を `packaging.tags` の優先順位で選ぶ → ハッシュ検証付きでダウンロードする（キャッシュあり）。
- sdist しか無い場合:
  - ホストと Target が一致していれば、PEP 517 で wheel をビルドする。
  - クロスビルドならエラーにする。`--find-links` で事前ビルド済みの wheel を指定してもらう。

### 6.3 インストール

- `installer` ライブラリで、Target のスキームへ展開する。
  - bundled: PBS の sysconfig パスを、ターゲット OS 用にパスを計算して使う（Windows は `Lib/site-packages`、Linux は `lib/python3.X/site-packages`）。
  - system: `purelib = platlib = lib/`、`scripts = bin/`、`data = .`。
- `RECORD`、`INSTALLER`（=`pybox <version>`）を書く。`direct_url.json`（PEP 610）は、ビルドしたホストの絶対パスが入って再現性を損なうため書かない。
- **console script は installer の生成物を使わない。** 絶対パスの shebang が入って再配置できないため、次のように置き換える。
  - アプリ自身の `console_scripts` / `gui_scripts`: バンドル直下に pybox ランチャを置く。
  - 依存パッケージのスクリプト（例: `pygmentize`）: `python/bin` / `python/Scripts` に、相対パスでインタプリタを探すスクリプトを書く。Linux は `/bin/sh` と Python の polyglot（`'''exec' "$(dirname -- "$(realpath -- "$0")")/python3.X" -I "$0" "$@"`）、Windows は `%~dp0..\python.exe` を呼ぶ `.cmd`。
- アプリ本体も wheel 化してから同じ経路でインストールする（「そのままインストールする」原則）。

---

## 7. ランチャ

- **Rust 製**の小さなバイナリ。std と最小限の crate（zip 展開など）だけを使い、数百 KB を目標にする。
- 事前ビルドしたもの（linux-gnu x86_64/aarch64、windows x86_64/aarch64、console/gui）を**pybox の wheel に同梱する**。distlib が `t64.exe` などを同梱しているのと同じ方式。こうすると pybox 本体は pure Python のままで、どのホストからでもクロスビルドできる。
- 設定は実行ファイルに直接埋め込まない。隣の `pybox.json`（exe 形式では payload 内）を読む。ランチャ名（実行ファイルの stem）で entry point を選ぶ、busybox 方式。名前が一致しない（リネームされた）場合は `default_entry_point` を使う。
- 実行するコマンドは `<interpreter> <python-flags> -c "import sys; sys.argv.pop(0); from mod import f; sys.exit(f())" <ランチャのパス> <ユーザー引数>`。`-c` のプレースホルダを取り除くので、`sys.argv[0]` はランチャのパスになり、argparse の `prog` も通常の console script と同じになる。コードは pybox が生成してマニフェストに書くので、ランチャは中身を解釈しない。
- ランチャ自体へのシンボリックリンク（`~/.local/bin` に置くなど）は、実体の場所まで解決してからバンドルを探す。
- 起動に失敗した場合は `pybox-launcher: ...` を stderr に出して終了コード 1 で終わる。Windows の GUI 版では、コンソールが無いのでメッセージボックスで表示する。
- プロセスモデル:
  - Linux: `execv` で python に置き換わる。PID とシグナルの扱いが自然になる。
  - Windows: 子プロセスとして起動して、その終了コードを返す。Ctrl-C / Ctrl-Break は親では無視し、コンソールを共有する子に任せる。子は Job Object（`KILL_ON_JOB_CLOSE | SILENT_BREAKAWAY_OK`）に入れ、ランチャが強制終了されたら Python も終わるようにする。SILENT_BREAKAWAY_OK により、Python がさらに起動したプロセスは Job に入らないので、アプリが常駐プロセスを起動しても巻き込まれない。
  - 将来の選択肢として、Windows で `python3XX.dll` をロードして `Py_Main` を呼ぶ、1 プロセス方式も検討する。
- デバッグ用の環境変数（アプリの引数と衝突しないよう、引数ではなく環境変数で受け取る）:
  - `PYBOX_DEBUG=1`: 探索・展開の様子をログ出力する。
  - `PYBOX_BOOT=python`: entry point ではなく同梱 Python の REPL / 引数をそのまま実行する。
  - `PYBOX_CACHE_DIR`: キャッシュディレクトリを上書きする。
- Windows の長いパス対策: キャッシュパスを短く保ち、展開時は `\\?\` プレフィックスを使う。

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

[tool.pybox]
lock = "pylock.toml"
python = "3.12"                  # requires-python の範囲内であることを検証する
interpreter = "bundled"          # bundled | system
python-flags = ["-I"]
compile-bytecode = true
prune = ["**/tests/**", "**/*.pyi"]

[tool.pybox.pbs]
release = "20251014"
flavor = "install_only_stripped"
strip = ["dev", "test", "idlelib"]   # 既定値。pip / ensurepip は常に削除

[[tool.pybox.target]]
name = "linux"
platform = "linux-x86_64"
glibc = "2.28"                   # 受け入れる manylinux の上限
formats = ["exe", "archive"]

[[tool.pybox.target]]
name = "windows"
platform = "windows-x86_64"
formats = ["exe", "archive"]

[[tool.pybox.target]]
name = "lambda"
platform = "lambda-arm64"        # glibc や python の既定を Lambda に合わせたプリセット
interpreter = "system"
formats = ["lambda-zip", "lambda-layer"]

[tool.pybox.lambda]
handler = "myapp.aws:handler"
```

## 9. CLI

```
pybox build [--target NAME ...] [--format FMT ...] [-o DIST]   # メインコマンド
pybox python list | fetch <ver> --platform <p>                 # PBS の管理
pybox targets                                                  # 解決後の Target（タグ・マーカー）を表示
pybox inspect <artifact>                                       # マニフェスト・同梱パッケージ・サイズ内訳
pybox run [--target NAME] -- <args>                            # ホスト向けにビルドしてすぐ実行
pybox cache clean
```

---

## 10. マニフェスト `pybox.json`

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
  "build": {"tool": "pybox 0.1.0", "source_date_epoch": 1760000000}
}
```

ランチャとブートストラップが読む唯一の独自フォーマットなので、スキーマにバージョンを付けて互換性を管理する。

---

## 11. 実装構成

pybox 本体は pure Python（3.11 以上、`tomllib` を使うため）。依存は `packaging`（26.1 以上。PEP 751 の `packaging.pylock` を使う）、`installer`、`build`、`truststore`。HTTP は標準ライブラリの `urllib` で済ませる。証明書の検証には `truststore` で OS のトラストストアを使う。python-build-standalone 系のインタプリタは、OpenSSL の既定設定ではシステムの CA を見つけられない環境があるため。

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
│   └── pybox-launcher/   # Rust ランチャ（bin: console / gui）
├── packages/
│   └── pybox/            # Python 本体（配布する wheel）
│       ├── pyproject.toml
│       └── src/pybox/...
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
pybox = { workspace = true }

[dependency-groups]
dev = ["pybox", "pytest", "ruff", "mypy"]
```

**併存させるときの注意点**

- **生成物の置き場所が衝突しない**: cargo は `target/` を、uv は `.venv/` を使う。`.gitignore` には `target/`、`.venv/`、`dist/`、`packages/pybox/src/pybox/_launchers/` を書く。
- **ランチャを wheel に入れる方法**: maturin の `bindings = "bin"` は**ホストのプラットフォーム専用の wheel**を作るので使わない。全ターゲット分のランチャを 1 つの `py3-none-any` wheel に入れるため、次の手順にする。
  1. CI で `cargo build --release --target <triple>` を各ターゲット向けに実行する。Linux は `cargo-zigbuild` で glibc 2.17 向けに作る。Windows は Windows ランナーで作る（Linux から作るなら `cargo-xwin` を使う）。
  2. できたバイナリを `packages/pybox/src/pybox/_launchers/<triple>/` に配置する（git には入れない）。
  3. ビルドバックエンド（hatchling）の `artifacts` で wheel に含める。
- **開発中の参照先**: `PYBOX_LAUNCHER_DIR` が指定されていればそこを使い、無ければリポジトリの `target/{debug,release}/` を探す。そのため、`cargo build` → `uv run pybox build` の流れで、ランチャを配置し直さずに試せる。
- **サンプルアプリ**: `tests/fixtures/apps/*` はそれぞれ独立したプロジェクト（自前の `pylock.toml` を持つ）なので、`exclude` で workspace から外す。

### 11.2 Python パッケージのモジュール構成

```
packages/pybox/src/pybox/
├── cli.py            # argparse
├── config.py         # pyproject 読み込み・検証・既定値
├── target.py         # Target → packaging.tags / marker 環境の合成、プリセット（lambda-* 等）
├── lock/
│   ├── __init__.py   # Artifact（選択結果の共通表現）
│   ├── pylock.py     # PEP 751: packaging.pylock.Pylock.select() に Target の環境とタグを渡す
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
├── boot/_pybox_boot.py
└── _launchers/       # 事前ビルド済みバイナリ（CI で配置して wheel に同梱。git には入れない）
```

## 12. 実装フェーズ

| Phase | 内容 | ねらい |
|---|---|---|
| 0 ✅ | Target モデル、pylock リーダ、wheel 選択、fetch、installer によるインストール、`lambda-zip` / `lambda-layer`、決定的な zip | ランチャ不要ですぐ役立つ。クロスビルドの土台を検証する |
| 1 ✅ | PBS 取得、bundled の `dir` / `archive`、Rust ランチャ（dir モード、Linux / Windows） | PyInstaller onedir の代わりになる |
| 2 | `exe`（payload 連結、キャッシュ展開）、system モードの `dir` / `pyz` | 単一ファイル配布 |
| 3 | `lambda-runtime`、bytecode 事前コンパイルの自動化、prune、SBOM、gui_scripts | Lambda の最適化と仕上げ |
| 4 | Windows の Authenticode 署名手順、`Py_Main` 埋め込み方式の検討、macOS | 拡張 |

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

1. **ツール名**: 当面は `pybox` を使う（PyPI へ公開する前に名前の衝突を確認する）。
2. **ランチャの言語**: Rust。
3. **bundled モードのインストール先**: PBS 自身の site-packages。
4. **pip**: 同梱しない（PBS から `pip` / `ensurepip` を常に削除する）。
5. **ロックが無い場合**: §6.1 のとおり、pip installation report を使う経路を許す（再現性は保証しないと警告を出す）。
6. **musl / Alpine**: 現時点では対象外。
7. **リポジトリ構成**: トップに cargo workspace と uv workspace を併存させる（§11.1）。

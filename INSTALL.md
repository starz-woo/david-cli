# `david` 전역 명령 설치 (v0.2.0)

Python 3.11 이상이 필요합니다. 실행 패키지와 prompt_toolkit/pathspec/wcwidth의 플랫폼 독립 wheel을 ZIP의 `wheels/`에 포함했습니다. 인터넷과 빌드 도구 다운로드 없이 설치할 수 있습니다. Python과 pip 자체는 포함하지 않으므로 먼저 준비하세요. 0.1.1과 달리 소스 직접 실행도 의존성 설치가 필요합니다.

## 1. 한 번 설치

압축 해제한 `david-cli` 폴더에서 실행합니다. 아래는 Windows 기준이며 macOS/Linux에서는 `python` 대신 `python3`를 사용하세요.

```sh
python -m pip install --user --no-index --find-links wheels --require-hashes -r requirements-lock.txt
```

이름이 같은 기존 `david` 명령을 사용 중이라면 먼저 충돌을 확인하세요. 이 설치는 `david-coding-agent` 패키지와 `david` 콘솔 명령을 설치합니다. PyPI에 게시된 같은 이름의 패키지를 설치하는 명령이 아닙니다.

이미 가상환경을 활성화한 상태라면 `--user`를 빼고 설치하세요. 해당 가상환경의 명령은 그 환경을 활성화한 터미널에서 사용할 수 있습니다. 시스템 Python이 사용자 설치를 차단하면 `--break-system-packages`를 사용하지 말고 아래 uv 방식으로 설치하세요.

### 명령을 찾지 못할 때: PATH 등록

pip가 출력하는 Scripts/bin 경로가 PATH에 있어야 합니다. 터미널을 새로 열어도 `david --help`가 안 되면 다음을 한 번 설정하세요.

**Windows PowerShell — 현재 터미널과 사용자 PATH에 등록**

```powershell
$bin = python -c "import sysconfig; print(sysconfig.get_path('scripts', scheme='nt_user'))"
$old = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($old -split ';') -notcontains $bin) {
    [Environment]::SetEnvironmentVariable('Path', "$old;$bin", 'User')
}
$env:Path = "$bin;$env:Path"
david --help
```

사용자 PATH만 변경하며 시스템 PATH는 변경하지 않습니다. 다른 터미널에는 새 창부터 반영됩니다. 위 Windows 설치/PATH 절차는 실제 Windows 장비 검증이 아직 필요합니다.

**macOS / Linux — 사용자 scripts 경로 확인**

```sh
python3 -c "import sysconfig; print(sysconfig.get_path('scripts', scheme='posix_user'))"
```

출력된 절대 경로를 사용해 `export PATH="출력된경로:$PATH"`를 셸 설정 파일(`~/.zshrc` 또는 `~/.bashrc`)에 한 번 추가하고 새 터미널을 여세요. 일반 경로를 추측하지 않고 설치한 Python의 실제 경로를 사용해야 합니다.

### uv가 이미 설치되어 있다면 (별도 격리 환경)

```sh
uv tool install --no-index --find-links wheels wheels/david_coding_agent-0.2.0-py3-none-any.whl
uv tool update-shell
```

새 터미널을 여세요. `uv tool install`의 전용 가상환경은 다른 프로젝트 Python 의존성과 분리됩니다.

## 2. 공통 서버 설정

```sh
david --init
david --config-path
```

생성되는 설정 위치:

- Windows: `%USERPROFILE%\.david\setting.json`
- macOS/Linux: `~/.david/setting.json`

`--init`은 기존 파일을 덮어쓰지 않습니다. 출력된 파일의 `model.base_url`, `model.name`을 사내 서버 정보로 바꾸세요. 기존 ZIP의 `setting.json`을 이미 편집했다면 그 내용을 이 파일로 직접 옮겨도 됩니다. 기존 상대 경로 `system_prompt_file`은 **새 설정 파일의 디렉터리 기준**으로 해석되므로 프롬프트 파일도 옮기거나 절대 경로를 지정하세요.

## 3. 어느 프로젝트에서나 실행

```sh
cd my-project
david
```

`david`를 실행한 현재 폴더가 작업 범위가 됩니다. 화면에 작업 경로를 표시하며 파일 수정 승인·명령 실행 권한은 그대로 유지됩니다. 프로젝트 내부의 `setting.json`은 자동으로 읽지 않습니다.

```sh
david --read-only
david --workspace 다른프로젝트경로
david --settings 별도설정파일.json
david --check
```

`--check`는 설정 구조 검증만 수행하며 실제 서버에 연결하지 않습니다. `/reload`로 공통 설정을 다시 읽으면 새 메모리 세션이 시작됩니다. 이전 변경은 디스크에 남으며 메모리 undo는 초기화됩니다. 필요하면 먼저 `/save on`으로 저장하세요.

의존성을 설치한 뒤 소스 실행 `python david.py --workspace ...`도 가능합니다. 이 방식은 **소스 폴더의 `setting.json`**을 기본값으로 사용합니다. 전역 `david`의 사용자 공통 설정과 구분하세요. 대화형 터미널은 TUI가 기본이며 `--plain`으로 기존 콘솔을 사용할 수 있습니다.

## 프로젝트 의존성과 분리하여 설치

사용자 설치 대신 새 가상환경을 만들어 위 pip 명령의 `--user`를 빼고 실행할 수 있습니다.

```sh
python -m venv .venv
```

Windows PowerShell: `.venv\Scripts\Activate.ps1`, CMD: `.venv\Scripts\activate.bat`, macOS/Linux: `source .venv/bin/activate`. 활성화가 어려우면 `.venv\Scripts\python.exe -m pip ...`처럼 해당 인터프리터를 직접 호출하세요. 정책을 우회하거나 시스템 Python을 덮어쓰지 마세요.

명령 recipe의 `{python}`은 David를 설치한 인터프리터입니다. 프로젝트별 pytest/빌드 환경은 `commands.<name>.argv[0]`에 해당 프로젝트 Python/실행 파일의 절대 경로를 지정하세요.

## 제거

pip 사용자 설치:

```sh
python -m pip uninstall david-coding-agent
```

uv 설치:

```sh
uv tool uninstall david-coding-agent
```

사용자 `~/.david/setting.json`은 업그레이드/제거 과정에서 자동 삭제하지 않습니다. 공용 Python Scripts/bin PATH도 다른 도구가 사용할 수 있으므로 무작정 제거하지 마세요.

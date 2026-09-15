"""Firestoreエミュレータを使う結合テストの共通基盤。

これまでのテストは協調する関数を差し替える方式で、「呼び出しの順序と条件分岐」は固定できても
「Firestoreが実際にどう振る舞うか」に依存する部分（トランザクションによる採番、create()による
一意性、バッチ書き込み、削除の消し残し）は検証できなかった。2026-09-15に萬年環境で起きた
同一GASの12件重複登録は、まさにこの層の不具合だった。

Googleが配布するFirestoreエミュレータを使い、本物と同じ挙動をローカルで検証する。
エミュレータはJava製のため、Javaが無い環境ではこれらのテストは自動的にスキップする
（差し替え方式のテストだけが動く。CIや他の端末でテスト全体が落ちないようにするため）。
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ID = "savepoint-test"
# Homebrew版OpenJDKはPATHに自動で入らない（keg-only）ため、既定の場所も探す。
HOMEBREW_JDK_BIN = "/opt/homebrew/opt/openjdk/bin"
STARTUP_TIMEOUT_SECONDS = 60


def _java_runs(env: dict[str, str]) -> bool:
    """javaが実際に起動できるかを確かめる。

    macOSにはJDK未導入でも/usr/bin/javaというスタブが存在し、存在確認だけでは
    「javaがある」と誤判定する（実行すると「Java 8+ JREが必要」と言って失敗する）。
    パスの有無ではなく-versionが成功するかで判定する。
    """
    try:
        return subprocess.run(
            ["java", "-version"], env=env, capture_output=True, timeout=30
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _java_available_env() -> dict[str, str] | None:
    """javaを実行できる環境変数一式を返す。見つからなければNone。"""
    env = os.environ.copy()
    # Homebrew版OpenJDKはkeg-onlyでPATHに入らないため、あれば優先的に前へ出す。
    if Path(HOMEBREW_JDK_BIN, "java").exists():
        homebrew_env = dict(env, PATH=f"{HOMEBREW_JDK_BIN}:{env.get('PATH', '')}")
        if _java_runs(homebrew_env):
            return homebrew_env
    return env if _java_runs(env) else None


def _free_port() -> int:
    """空きポートを取得する（並行実行や前回の残骸との衝突を避ける）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_ready(host_port: str, process: subprocess.Popen) -> bool:
    """エミュレータが応答を返すまで待つ。起動に失敗したらFalse。"""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://{host_port}/", timeout=1):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def firestore_emulator() -> str:
    """セッション中1回だけエミュレータを起動し、ホスト:ポートを返す。

    FIRESTORE_EMULATOR_HOSTが既に設定されていれば、起動済みのものをそのまま使う
    （開発中に手元で起動しっぱなしにしている場合や、CIが別途用意している場合）。
    """
    if existing := os.environ.get("FIRESTORE_EMULATOR_HOST"):
        os.environ.setdefault("GCP_PROJECT", PROJECT_ID)
        yield existing
        return

    env = _java_available_env()
    if env is None:
        pytest.skip("Javaが無いためFirestoreエミュレータを起動できない（brew install openjdk）")
    if not shutil.which("gcloud"):
        pytest.skip("gcloud CLIが無いためFirestoreエミュレータを起動できない")

    host_port = f"127.0.0.1:{_free_port()}"
    process = subprocess.Popen(
        ["gcloud", "emulators", "firestore", "start", f"--host-port={host_port}"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # gcloudはJavaを子プロセスとして起動するため、終了時にプロセスグループごと止める。
        start_new_session=True,
    )

    if not _wait_until_ready(host_port, process):
        _terminate(process)
        pytest.skip("Firestoreエミュレータの起動に失敗した（gcloud components install cloud-firestore-emulator）")

    os.environ["FIRESTORE_EMULATOR_HOST"] = host_port
    os.environ["GCP_PROJECT"] = PROJECT_ID
    try:
        yield host_port
    finally:
        _terminate(process)
        os.environ.pop("FIRESTORE_EMULATOR_HOST", None)


def _terminate(process: subprocess.Popen) -> None:
    """エミュレータをプロセスグループごと確実に終了させる。"""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=10)
    except (ProcessLookupError, PermissionError):
        pass
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)


@pytest.fixture
def fs(firestore_emulator):
    """テストごとに空のFirestoreを用意し、共有クライアントを返す。

    firestore_client.db()はプロセス内で1個のクライアントを使い回すが、そのクライアントは
    生成時にFIRESTORE_EMULATOR_HOSTを読むため、エミュレータ起動より先に本番向けの
    クライアントが作られていると接続先が変わらない。キャッシュを明示的に破棄する。
    """
    import firestore_client

    firestore_client._client = None
    client = firestore_client.db()
    _clear(firestore_emulator)
    yield client
    firestore_client._client = None


def _clear(host_port: str) -> None:
    """エミュレータの全データを消す（テスト間で状態を持ち越さない）。"""
    url = f"http://{host_port}/emulator/v1/projects/{PROJECT_ID}/databases/(default)/documents"
    request = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except urllib.error.URLError as exc:  # pragma: no cover - 異常時のみ
        raise RuntimeError(f"エミュレータのデータ削除に失敗した: {exc}") from exc

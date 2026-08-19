"""
로컬 new.txt 를 GitHub API 로 직접 커밋한다.  (완전 자동화)

git push 가 사내 프록시에 막히는 환경에서, api.github.com 은 열려 있는 것을
확인하고 만든 경로다. 평범한 HTTPS JSON 요청이라 프록시를 통과한다.

준비 (최초 1회)
--------------
1. https://github.com/settings/personal-access-tokens/new
   - Token name        : marinesia-vessel-register
   - Repository access : Only select repositories -> fetch_marimesia
   - Permissions       : Repository permissions -> Contents -> Read and write
   - Expiration        : 90 days 권장
2. 발급된 토큰(github_pat_ 로 시작)을 이 스크립트와 같은 폴더의
   token.txt 에 한 줄로 저장한다.
   또는 환경변수:  setx GITHUB_TOKEN "github_pat_xxxx"

사용법
------
    Register-NewVessels.cmd
    python register_new_vessels.py
    python register_new_vessels.py D:\\temp\\list.txt
    python register_new_vessels.py --dry-run       검증만
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OWNER = "lws2013"
REPO = "fetch_marimesia"
BRANCH = "main"
TARGET_PATH = "input/new.txt"
COMMIT_MESSAGE = "Register new vessels"

DEFAULT_INPUT = Path(r"C:\Work\AIS\new.txt")
ARCHIVE_DIR = DEFAULT_INPUT.parent / "sent"

API_BASE = f"https://api.github.com/repos/{OWNER}/{REPO}"
CONTENTS_URL = f"{API_BASE}/contents/{TARGET_PATH}"
ACTIONS_URL = f"https://github.com/{OWNER}/{REPO}/actions"

SCIENTIFIC = re.compile(r"^\d(\.\d+)?[eE]\+?\d+$")


def make_context() -> ssl.SSLContext:
    """
    사내 프록시가 TLS를 가로채는 환경에서는 인증서 폐기(CRL) 조회가 실패한다.
    curl --ssl-no-revoke 와 같은 효과. 인증서 검증 자체는 유지한다.
    """
    return ssl.create_default_context()


def request(method: str, url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "marinesia-vessel-register")

    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=60, context=make_context()) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"message": raw[:400]}
    except urllib.error.URLError as exc:
        raise RuntimeError(f"네트워크 오류: {exc.reason}") from exc


def load_token() -> str:
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()

    if token:
        return token

    token_file = Path(__file__).resolve().with_name("token.txt")

    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()

        if token:
            return token

    print("[오류] GitHub 토큰을 찾을 수 없습니다.\n")
    print("  1. https://github.com/settings/personal-access-tokens/new")
    print("     Repository access : Only select repositories -> fetch_marimesia")
    print("     Permissions       : Contents -> Read and write")
    print("  2. 발급된 토큰을 아래 파일에 한 줄로 저장하세요.")
    print(f"     {token_file}")
    sys.exit(1)


def read_any_encoding(path: Path) -> str:
    raw = path.read_bytes()

    for encoding in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    return raw.decode("utf-8", errors="replace")


def clean(value: str) -> str:
    value = value.replace("\u3000", " ").replace("\ufeff", "")
    value = "".join(
        chr(ord(ch) - 0xFEE0) if "０" <= ch <= "９" else ch for ch in value
    )
    return value.strip()


def imo_check_digit_ok(imo: str) -> bool:
    return sum(int(imo[i]) * (7 - i) for i in range(6)) % 10 == int(imo[6])


def classify(token: str):
    value = clean(token).upper()

    if value.startswith("IMO"):
        value = value[3:].strip()

    value = value.replace("-", "").replace(" ", "")

    if not value:
        return "", "", "식별번호 없음"

    if SCIENTIFIC.match(value):
        return "", value, (
            f"지수 표기 '{value}' · Excel에서 해당 열을 '텍스트' 서식으로 "
            "바꾼 뒤 다시 복사하세요"
        )

    if not value.isdigit():
        return "", value, f"숫자가 아닌 문자 포함 '{value}'"

    if len(value) == 7:
        if not imo_check_digit_ok(value):
            return "", value, f"IMO 체크디지트 불일치 '{value}' · 오타 가능성"
        return "imo", value, ""

    if len(value) == 9:
        mid = int(value[:3])
        if not (201 <= mid <= 775):
            return "mmsi", value, f"MID {mid} 는 선박용 범위(201-775) 밖 · 확인 필요"
        return "mmsi", value, ""

    return "", value, f"IMO 7자리 / MMSI 9자리가 아님 ({len(value)}자리) '{value}'"


def validate(text: str):
    rows, errors, warnings = [], [], []

    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue

        parts = line.split("\t")
        if len(parts) < 2:
            parts = re.split(r"\s{2,}", line.strip())

        if len(parts) < 2:
            errors.append(f"{lineno}행 · 탭 구분자 없음 -> {line.strip()}")
            continue

        name = clean(parts[0])

        if not name:
            errors.append(f"{lineno}행 · 선박명 없음")
            continue

        if name.lower() in {"vessel_name", "vessel", "선박명", "name"}:
            continue

        imo, mmsi, msgs = "", "", []

        for token in parts[1:]:
            if not clean(token):
                continue

            kind, value, err = classify(token)

            if kind == "imo":
                imo = value
            elif kind == "mmsi":
                mmsi = value

            if err:
                msgs.append(err)

        if not imo and not mmsi:
            errors.append(f"{lineno}행 ({name}) · {'; '.join(msgs) or '식별번호 없음'}")
            continue

        for m in msgs:
            warnings.append(f"{lineno}행 ({name}) · {m}")

        if not imo:
            warnings.append(f"{lineno}행 ({name}) · IMO 없이 MMSI만 등록됩니다")

        rows.append((name.upper(), imo, mmsi))

    return rows, errors, warnings


def main() -> int:
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv

    src = Path(positional[0]) if positional else DEFAULT_INPUT

    print("=" * 60)
    print(" 신규 선박 등록")
    print("=" * 60)
    print(f"\n입력 파일: {src}")

    if not src.exists():
        print("\n[오류] 파일이 없습니다.\n")
        print("  형식 (탭 구분, 헤더 없음):")
        print("      MAERSK TAURUS\t9784089")
        print("      UNI PERFECT\t357979000")
        return 1

    rows, errors, warnings = validate(read_any_encoding(src))

    print(f"\n[검증] 유효 {len(rows)}행 · 오류 {len(errors)}건 · 경고 {len(warnings)}건")

    if rows:
        print()
        print("  선박명                          IMO        MMSI")
        print("  " + "-" * 52)
        for name, imo, mmsi in rows[:40]:
            print(f"  {name[:30]:<30}  {imo or '-':<9}  {mmsi or '-'}")
        if len(rows) > 40:
            print(f"  ... 외 {len(rows) - 40}행")

    if warnings:
        print("\n[경고]")
        for w in warnings:
            print(f"  · {w}")

    if errors:
        print("\n[오류] 아래 행은 등록되지 않습니다.")
        for e in errors:
            print(f"  · {e}")

    if not rows:
        print("\n유효한 행이 없습니다. 파일을 수정한 뒤 다시 실행하세요.")
        return 1

    normalized = "\n".join(
        f"{n}\t{i}\t{m}" if (i and m) else f"{n}\t{i or m}" for n, i, m in rows
    ) + "\n"

    if dry_run:
        print("\n[--dry-run] 검증만 수행했습니다.")
        return 0

    token = load_token()

    print(f"\n{len(rows)}척을 등록합니다.")

    if input("계속할까요? [y/N] ").strip().lower() != "y":
        print("취소되었습니다.")
        return 0

    print("\n[1/3] 원격 상태 확인")
    status, payload = request("GET", f"{CONTENTS_URL}?ref={BRANCH}", token)

    if status == 401:
        print("      [오류] 토큰이 유효하지 않습니다 (401).")
        print("      만료되었거나 잘못 복사되었을 수 있습니다.")
        return 1

    if status == 403:
        print("      [오류] 권한 부족 (403).")
        print("      토큰에 Contents: Read and write 권한이 필요합니다.")
        print(f"      메시지: {payload.get('message')}")
        return 1

    sha = None

    if status == 200:
        sha = payload.get("sha")
        print("      기존 new.txt 가 있어 덮어씁니다.")
    elif status == 404:
        print("      신규 파일로 생성합니다.")
    else:
        print(f"      [오류] 예상치 못한 응답 {status}: {payload.get('message')}")
        return 1

    print("[2/3] 커밋")
    body = {
        "message": COMMIT_MESSAGE,
        "content": base64.b64encode(normalized.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }

    if sha:
        body["sha"] = sha

    status, payload = request("PUT", CONTENTS_URL, token, body)

    if status not in (200, 201):
        print(f"      [오류] 커밋 실패 ({status})")
        print(f"      {payload.get('message')}")
        if status == 409:
            print("      다른 작업과 충돌했습니다. 잠시 후 다시 실행하세요.")
        return 1

    commit_sha = payload.get("commit", {}).get("sha", "")
    print(f"      {commit_sha[:7]}  {COMMIT_MESSAGE}")

    print("[3/3] 로컬 파일 보관")
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        src.replace(ARCHIVE_DIR / f"new_{stamp}.txt")
        print(f"      {ARCHIVE_DIR}\\new_{stamp}.txt")
    except OSError as exc:
        print(f"      [경고] 보관 실패: {exc}")
        print("      new.txt 를 직접 정리하세요. 그대로 두면 다음에 중복 등록됩니다.")

    print("\n" + "=" * 60)
    print(" 등록 완료")
    print("=" * 60)
    print("\n  Ingest New Vessels 워크플로우가 자동 실행됩니다.")
    print("  1~2분 뒤 결과를 확인하세요.")
    print(f"  {ACTIONS_URL}")

    return 0


if __name__ == "__main__":
    try:
        code = main()
    except RuntimeError as exc:
        print(f"\n[오류] {exc}")
        code = 1
    except KeyboardInterrupt:
        print("\n취소되었습니다.")
        code = 0

    input("\n엔터를 누르면 종료합니다...")
    sys.exit(code)

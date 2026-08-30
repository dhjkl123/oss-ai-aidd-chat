---
title: aidd-chat
emoji: 💬
colorFrom: gray
colorTo: blue
sdk: docker
app_port: 7860
license: other
license_name: UNRESOLVED-see-deploy-readme
pinned: false
short_description: 익명 한국어 Q&A 대화 Shell
---

# Hugging Face Space Manifest

A Space Repo's **root** `README.md` is its Manifest, so this file cannot be this
repo's `README.md` -- it has to be copied to the Space repo's root, beside the
`Dockerfile` this repo already builds. That is the only difference between the two
deployments: **one Image, two Manifests.**

> **`license:` 값은 아직 정해지지 않았다.** 이 repo에는 `LICENSE` 파일도 `pyproject.toml`의
> `license` Key도 없다. 그래서 여기에 실제 License를 적을 수 없었고, 지어내지도 않았다 --
> `license_name: UNRESOLVED-see-deploy-readme`는 **공개 배포 전에 반드시 교체해야 하는
> 자리표시자**이며, Space를 공개하기 전에 repo의 License를 먼저 정해야 한다. License 증거는
> Story 1.11이 생산하지 않았다(`story-1-11-evidence.md` §9).

## 선언

| 값 | 이 Manifest | 어디서 같은 값을 말하는가 |
|----|------------|--------------------------|
| `sdk` | `docker` | -- |
| `app_port` | `7860` | `Dockerfile`의 `EXPOSE 7860`과 `--port 7860` |
| Worker | 1 | `Dockerfile`의 `--workers 1`, `.env.example`의 `WEB_CONCURRENCY=1` |
| Replica | 1 | `.env.example`의 `REPLICA_COUNT=1` |
| UID | 1000 | `Dockerfile`의 `USER 1000` |

<!-- Worker·Replica 선언. Space Manifest Frontmatter에는 이 둘을 표현하는 Key가
     없으므로(Space는 Replica를 Hardware 설정으로 다룬다) 여기 본문에 명시하고,
     `tests/test_story_1_11.py`가 이 파일·`Dockerfile`·`.env.example` 세 곳의 숫자가
     같은지 확인한다. 셋 중 하나만 바뀌면 Test가 실패한다.
     WEB_CONCURRENCY=1
     REPLICA_COUNT=1 -->

**Worker 1개·Process 1개·Replica 1개를 넘겨서는 안 된다.** 이 앱의 모든 상한
(상주 Conversation, 동시 Run, Rate, Spend Circuit Breaker)과 모든 상태(대화, Run,
Replay Log, Idempotency Receipt)는 **In-memory**다. Worker나 Replica가 둘이 되면
상한은 조용히 나뉘고 Conversation은 요청마다 다른 Process에 떨어진다. 외부 공유
TTL Store 없이 Horizontal Scale은 금지다 -- `public_demo` Profile은 `WEB_CONCURRENCY`
와 `REPLICA_COUNT`가 정확히 `1`로 **선언**되지 않으면 기동을 거부한다(선언일 뿐
실제 Topology를 확인하지는 못한다).

## Reverse Proxy -- 이 둘을 빼면 앱이 동작하지 않는다

Space는 TLS를 종료하는 Router 뒤에 앱을 둔다. 그러면 Container가 받는 요청의 Scheme은
`http`이고 Client가 보낸 `Origin`은 `https://…`다. 이 앱의 Origin Gate는
`scheme://netloc`을 **정확히** 비교하므로, Router의 `X-Forwarded-Proto`를 믿지 않으면
`POST /api/v1/conversations`·`POST …/runs`·`POST …/cancel`이 **전부 403
`invalid_origin`**이 된다. `/live`·`/ready`·`/`는 GET이라 멀쩡히 통과하므로,
**GET만 확인하는 Smoke는 완전히 망가진 배포를 통과시킨다.**

| 환경변수 | 값 | 왜 |
|----------|-----|-----|
| `FORWARDED_ALLOW_IPS` | `*` | uvicorn이 `X-Forwarded-Proto`/`-For`를 읽게 한다. `--proxy-headers`는 Dockerfile CMD에 이미 있고 uvicorn 기본값이지만, 이 변수가 없으면 어떤 Peer의 Header도 신뢰하지 않는다. Space에서 Container에 직접 접속하는 Peer는 플랫폼 Router뿐이므로 `*`가 안전하다. Container를 직접 노출한다면 절대 쓰지 말 것. |
| `TRUSTED_PROXY_HOPS` | `1` | 이 값이 `0`이면 Client IP가 **언제나 Router**가 되어 모든 방문자가 하나의 per-IP Rate 예산을 공유한다. 한 사람이 나머지 전부를 `rate_limited`로 만든다. `1`은 `X-Forwarded-For`의 마지막 한 칸만 벗겨 읽는다. |

## Disk

Ephemeral로 취급한다. Persistent Storage를 요구하지 않으며 Container가 재시작하면
이전 대화·Run은 남지 않는다(그것이 이 앱의 만료 계약과 같은 방향이다).

## 환경변수

Space Settings의 **Secrets/Variables**로만 넣는다. Image에는 굽지 않는다 -- Dockerfile의
COPY 허용목록에 `.env`가 없고, `.dockerignore`가 Build Context에서도 제외한다. 이름·유효값·
범위는 이 repo의 [`.env.example`](../../.env.example)에 있고, 공개 배포는
`DEPLOYMENT_PROFILE=public_demo`가 되어야 한다. 용량·Rate 변수 17개와
`MAX_ATTEMPTS_PER_LINEAGE`는 기본값이 없어 채우지 않으면 `/ready`가 계속 503이다.

> **이 Manifest는 공개 Provider를 고르지 않는다.** `public_demo`는 Private·Loopback·
> Link-local Endpoint를 거부하므로, Story 1.11이 실측에 쓴 Private LAN Ollama
> Binding은 여기에 넣을 수 없고 그 결과를 공개 Release Evidence로 재사용할 수도 없다.
> 공개 Provider 선택·Credential 준비·실제 Space 배포·공개 URL `/ready` 확인은 Story
> 1.11 범위 밖이며 아직 수행되지 않았다.

## 배포 절차

1. Space를 `Docker` SDK로 만든다.
2. 이 파일을 Space repo 루트 `README.md`로 복사한다.
3. 이 repo에서 **Build에 필요한 파일 전부**를 Space repo 루트에 그대로 둔다
   -- 같은 Image여야 한다:
   - `Dockerfile`
   - `.dockerignore` (Build Context에서 Credential을 빼는 두 번째 방어선)
   - `pyproject.toml`, `uv.lock`, `.python-version` (Dockerfile이 COPY하는 셋)
   - `src/` (Dockerfile이 COPY하는 넷째)

   이 repo의 `README.md`는 **복사하지 않는다.** Space repo 루트의 `README.md`는
   Manifest이고, Dockerfile은 어떤 README도 COPY하지 않는다.
4. `.env.example`의 변수를 Space Secrets/Variables로 채운다. 위 Reverse Proxy 절의
   `FORWARDED_ALLOW_IPS`·`TRUSTED_PROXY_HOPS`를 **반드시 포함한다.**
5. Smoke -- **GET만 보지 말 것**:
   - `GET /live` → 204, `GET /ready` → 204, `GET /` → 200
   - `POST /api/v1/conversations` (`Origin: https://<space-url>` 포함) → **201**.
     403 `invalid_origin`이면 4번의 Forwarding 설정이 빠진 것이다.
   - 이어서 `POST /api/v1/conversations/{id}/runs`로 질문 한 건이 `completed`까지
     가는지 확인한다.

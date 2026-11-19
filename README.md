# AI 여친 디스코드 봇 (Ollama, 로컬)

블루아카이브 풍 임시 캐릭터 "하루카제 미나"가 들어 있습니다. 캐릭터와 동작은 전부 `config/` 안의 파일로 바꿉니다.

## 구조

```
config/persona.yaml   캐릭터 (이름/성격/말투/배경/규칙/예시 대화)  ← 가장 중요
config/settings.yaml  모델, 생성 옵션, 기억, 타이핑 속도, 먼저 말걸기, 채널
bot/main.py           디스코드 이벤트, 슬래시 명령, 먼저 말걸기 루프
bot/chat.py           프롬프트 조립, 말풍선 분리, 장기 기억 추출
bot/llm.py            Ollama 호출
bot/memory.py         SQLite (대화 기록 + 장기 기억)
bot/persona.py        persona.yaml → 시스템 프롬프트
scripts/              설치 스크립트, systemd 서비스
```

## 설치 (Ubuntu / WSL)

1. 프로젝트 폴더를 우분투로 복사합니다. WSL이면 `cp -r /mnt/c/Users/snowm/Desktop/PR_AIgirl ~/PR_AIgirl` 후 그 폴더에서 진행합니다. `/mnt/c` 위에서 직접 돌리면 느립니다.
2. `bash scripts/setup_ubuntu.sh` 를 실행합니다. Ollama 설치, 모델 다운로드, venv 구성까지 처리합니다. 다른 모델을 쓰려면 `bash scripts/setup_ubuntu.sh 모델명` 으로 실행합니다.
3. 디스코드 개발자 포털(https://discord.com/developers/applications)에서 봇을 만듭니다.
   - Bot 탭에서 **Message Content Intent**를 켜고 토큰을 복사합니다.
   - OAuth2 → URL Generator에서 `bot`, `applications.commands` 스코프로 서버에 초대합니다.
4. `.env`에 `DISCORD_TOKEN=...` 을 넣습니다.
5. `config/settings.yaml`에서 본인 디스코드 ID를 `admin_user_ids`에 넣습니다. 개발자 모드를 켠 뒤 프로필을 우클릭해 ID를 복사합니다.
6. `.venv/bin/python -m bot.main` 으로 실행합니다. 봇에게 DM을 보내면 대답합니다.
7. 상시 구동은 `scripts/aigirl.service` 안의 안내대로 systemd에 등록합니다.

WSL에서 GPU를 쓰려면 Windows에 최신 NVIDIA 드라이버가 깔려 있어야 합니다. WSL 안에서는 `nvidia-smi`로 인식되는지 확인하세요. 상시 운영은 WSL보다 네이티브 우분투가 안정적입니다.

## 모델 추천 (12GB VRAM)

| 모델 | 용량 | 특징 |
|---|---|---|
| `gemma3:12b` (기본) | 약 8GB | 한국어가 자연스럽고 롤플레이 말투 유지가 좋음 |
| `qwen2.5:14b` | 약 9GB | 지시 따르기는 강하지만 한국어 말투가 조금 딱딱함 |
| `exaone3.5:7.8b` | 약 5GB | 한국어는 좋지만 표현력이 약함. 속도 우선일 때 |

모델은 `config/settings.yaml`의 `ollama.model`만 바꾸면 교체됩니다. 말이 너무 얌전하면 `temperature`를 올리고, 같은 말을 반복하면 `repeat_penalty`를 올립니다.

## 캐릭터 품질을 올리는 법

- `persona.yaml`의 `examples`가 가장 효과가 큽니다. 원하는 말투로 예시 대화를 10~20개까지 늘리세요.
- 캐릭터가 AI처럼 말하면 `rules`에 금지 표현을 구체적으로 적습니다.
- 수정 후 `/리로드`로 재시작 없이 반영됩니다. `settings.yaml`은 재시작이 필요합니다.

## 슬래시 명령

- `/기억보기`: 캐릭터가 기억하는 내용 확인
- `/기억초기화`: 내 대화 기록과 기억 삭제
- `/리로드`: (관리자) persona.yaml 다시 불러오기

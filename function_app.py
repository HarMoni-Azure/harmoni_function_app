import json
import os
import uuid
import datetime
import math
import logging

import azure.functions as func
from azure.storage.blob import BlobServiceClient


# =============================================================================
# Azure Functions Python v2 모델
# - FunctionApp 객체생성, 데코레이터(@app.route)로 HTTP 라우트 등록
# =============================================================================
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# =============================================================================
# 낙상 판정 임계값(Threshold)
# - fallScore >= THRESHOLD 이면 서버 판단 낙상(True)
# =============================================================================
THRESHOLD = 0.7
ENABLE_LOCAL_KEY_GUARD = False

# =============================================================================
# Blob 환경변수
# - Azure 배포: Function App > Configuration(Application settings)에 설정
# - 로컬 실행: local.settings.json의 Values에 설정
#
# 컨테이너 명은 local.settings.json과 Azure 포털 설정이 일치해야 함
# 컨테이너 명 변경시 반드시 둘다 변경 해주어야 함 > 현재설정은 테스트용 functoblob-data
# 로컬용 테스트 코드
# =============================================================================
BLOB_CONNECTION_STRING = os.getenv("BLOB_CONNECTION_STRING")
BLOB_CONTAINER_NAME = os.getenv("BLOB_CONTAINER_NAME")  # 예: functoblob-data


# =============================================================================
# BlobServiceClient는 전역에서 1회 생성 후 재사용 하는 방식
# =============================================================================
BLOB_SERVICE_CLIENT = (
    BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
    if BLOB_CONNECTION_STRING
    else None
)


def _to_float(x, name: str) -> float:
    """
    센서 값이 숫자인지 검사 후 float 변환
    현재 - 변환 불가/None이면 ValueError 발생 (400으로 처리)
    변경 - 
    """
    try:
        return float(x)
    except Exception:
        raise ValueError(f"{name} must be a number")


def _calc_fall_score(sensor: dict) -> float:
    """
    센서 기반 낙상 스코어(0~1) 계산

    입력(sensor):
      {
        "ax": ..., "ay": ..., "az": ...,
        "gx": ..., "gy": ..., "gz": ...
      }

    계산:
    - 가속도 크기(|a|)가 1g(9.81)에서 벗어날수록 위험 증가
    - 자이로 크기(|g|)가 커질수록 위험 증가
    - score = 0.7 * acc_score + 0.3 * gyro_score

    주의:
    - MVP 단계. 추후 Azure ML 모델로 교체 가능.
    """
    ax = _to_float(sensor.get("ax"), "sensor.ax")
    ay = _to_float(sensor.get("ay"), "sensor.ay")
    az = _to_float(sensor.get("az"), "sensor.az")

    gx = _to_float(sensor.get("gx"), "sensor.gx")
    gy = _to_float(sensor.get("gy"), "sensor.gy")
    gz = _to_float(sensor.get("gz"), "sensor.gz")

    # 벡터 크기 계산
    a_mag = math.sqrt(ax**2 + ay**2 + az**2)
    g_mag = math.sqrt(gx**2 + gy**2 + gz**2)

    # 가속도 편차(1g 기준) 정규화 (대략)
    acc_dev = abs(a_mag - 9.81)
    acc_score = min(1.0, acc_dev / 6.0)

    # 자이로 크기 정규화 (대략)
    gyro_score = min(1.0, g_mag / 6.0)

    score = 0.7 * acc_score + 0.3 * gyro_score
    return max(0.0, min(1.0, score))


def _upload_to_blob(body: dict) -> str:
    """
    요청 바디(JSON)> Blob Storage 파일 업로드 > 업로드된 경로 반환

    저장 정책(gpt추천):
    - Raw Zone 성격으로 "요청 원본 전체"를 저장
    - 컨테이너: BLOB_CONTAINER_NAME (예: functoblob-data)
    - 경로: fall-detection/yyyy/MM/dd/{uuid}.json
    - 파일 내용: JSON 문자열 (body 전체)
    // 추후 변경 가능

    실패 시:
    - 환경변수 누락 또는 업로드 실패 시 예외 발생 → 호출부에서 500 처리
    """
    # 필수 설정값 체크 (실패시 missing 반환)
    if not BLOB_SERVICE_CLIENT:
        raise RuntimeError("BLOB_CONNECTION_STRING is missing or invalid.")
    if not BLOB_CONTAINER_NAME:
        raise RuntimeError("BLOB_CONTAINER_NAME is missing.")

    # 애저 서버기준 날짜 파티션 >> 추후 Databricks Auto Loader에서 경로 파티션으로 활용 가능
    now = datetime.datetime.utcnow()
    blob_path = f"fall-detection/{now:%Y/%m/%d}/{uuid.uuid4()}.json"

    # 컨테이너/블랍 클라이언트 획득
    container_client = BLOB_SERVICE_CLIENT.get_container_client(BLOB_CONTAINER_NAME)
    blob_client = container_client.get_blob_client(blob_path)

    # 업로드
    # - uuid 파일명이라 중복 확률 거의 없지만, overwrite=True로 확실하게
    blob_client.upload_blob(json.dumps(body), overwrite=True)

    return blob_path


@app.route(route="http_trigger", methods=["POST"])
def http_trigger(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP Trigger 엔드포인트
    요청(JSON) 예시:
    {
      "timestamp": "2025-12-16T15:42:30.123Z",
      "sensor": {"ax":0.05,"ay":-0.12,"az":9.78,"gx":0.02,"gy":0.01,"gz":-0.03},
      "isFall": false
    }

    처리 흐름:
    1) (선택) 로컬 추가 키 가드
    2) JSON 파싱
    3) 스키마 검증(timestamp/sensor/isFall)
    4) fallScore 계산 및 서버판단 isFall 생성
    5) 원본 payload를 Blob에 저장 (Raw Zone)
    6) 결과 응답 반환 (fallScore, isFall, blobPath 포함)
    """

    # -------------------------------------------------------------------------
    # (선택) 로컬 추가 키 가드
    # -------------------------------------------------------------------------
    if ENABLE_LOCAL_KEY_GUARD:
        expected_key = os.getenv("FUNCTION_KEY")
        provided_key = req.headers.get("x-functions-key") or req.params.get("code")

        # expected_key가 설정되어 있을 때만 비교
        if expected_key and provided_key != expected_key:
            logging.warning("Unauthorized: local key guard failed")
            return func.HttpResponse("Unauthorized", status_code=401)

    # -------------------------------------------------------------------------
    # JSON 파싱
    # -------------------------------------------------------------------------
    try:
        body = req.get_json()
    except ValueError:
        logging.warning("BadRequest: Invalid JSON")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "Invalid JSON"}),
            status_code=400,
            mimetype="application/json",
        )

    # -------------------------------------------------------------------------
    # 스키마 검증 (timestamp / sensor / isFall)
    # -------------------------------------------------------------------------
    timestamp = body.get("timestamp")
    sensor = body.get("sensor")
    client_is_fall = body.get("isFall")

    logging.info(f"http_trigger called. timestamp={timestamp}, client_isFall={client_is_fall}")

    if not isinstance(timestamp, str):
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "'timestamp' must be an ISO8601 string"}),
            status_code=400,
            mimetype="application/json",
        )

    if not isinstance(sensor, dict):
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "'sensor' must be an object"}),
            status_code=400,
            mimetype="application/json",
        )

    if not isinstance(client_is_fall, bool):
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "'isFall' must be a boolean"}),
            status_code=400,
            mimetype="application/json",
        )

    # -------------------------------------------------------------------------
    # fallScore 계산 + 서버판단
    # -------------------------------------------------------------------------
    try:
        fall_score = _calc_fall_score(sensor)
    except ValueError as e:
        return func.HttpResponse(
            json.dumps({"status": "error", "message": str(e)}),
            status_code=400,
            mimetype="application/json",
        )

    server_is_fall = fall_score >= THRESHOLD
    logging.info(f"computed fallScore={fall_score:.4f}, threshold={THRESHOLD}, server_isFall={server_is_fall}")

    # -------------------------------------------------------------------------
    # Blob 업로드 (Raw 저장)
    # - 중요: POST 성공(200)인데 파일이 안 쌓이는 사고를 막으려면
    #         실제 라우트 함수 안에서 업로드를 수행 >> 변경할지?
    # -------------------------------------------------------------------------
    try:
        blob_path = _upload_to_blob(body)
    except Exception as e:
        logging.exception("Blob upload failed")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": f"Blob upload failed: {str(e)}"}),
            status_code=500,
            mimetype="application/json",
        )

    # -------------------------------------------------------------------------
    # 응답
    # - blobPath 저장여부 확인 쉽게
    # -------------------------------------------------------------------------
    resp = {
        "status": "ok",
        "threshold": THRESHOLD,
        "fallScore": round(fall_score, 4),
        "isFall": server_is_fall,
        "blobPath": blob_path,
        "received": {
            "timestamp": timestamp,
            "sensor": sensor,
            "isFall": client_is_fall,
        },
    }

    return func.HttpResponse(
        json.dumps(resp),
        status_code=200,
        mimetype="application/json",
    )


'''
이전 버전 코드
from azure.storage.blob import BlobServiceClient
import json, os, uuid, datetime
import math
import logging

import azure.functions as func


#blob 변수
blob_conn = os.environ["BLOB_CONNECTION_STRING"]
container = os.environ["BLOB_CONTAINER_NAME"]
# ------------------------------------------------------------
# Function App 설정
# - AuthLevel.FUNCTION: Function Key 없으면 401 (Azure 기본 보안)
#$env:FUNC_KEY="<Function Key 값>"
# ------------------------------------------------------------
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# ------------------------------------------------------------
# 설정값
# ------------------------------------------------------------
THRESHOLD = 0.7

# 로컬에서 "추가 인증"을 강제로 걸고 싶을 때만 사용 (선택)
# local.settings.json에 FUNCTION_KEY를 넣고,
# curl에 x-functions-key로 같은 값을 보내면 통과
# - 기본은 OFF 권장 (Azure 기본 Function Key 인증만으로도 충분)
ENABLE_LOCAL_KEY_GUARD = False

def main(req):
    blob_service_client = BlobServiceClient.from_connection_string(
        os.environ["BLOB_CONNECTION_STRING"]
    )

    container_client = blob_service_client.get_container_client(
        os.environ["BLOB_CONTAINER_NAME"]
    )

    body = req.get_json()

    now = datetime.datetime.utcnow()
    path = f"fall-detection/{now:%Y/%m/%d}/{uuid.uuid4()}.json"

    blob_client = container_client.get_blob_client(path)
    blob_client.upload_blob(json.dumps(body), overwrite=True)

    return "ok"

def _to_float(x, name: str) -> float:
    """센서 값이 숫자인지 검사 후 float 변환"""
    try:
        return float(x)
    except Exception:
        raise ValueError(f"{name} must be a number")


def _calc_fall_score(sensor: dict) -> float:
    """
    센서 기반 낙상 스코어(0~1) 계산 (간단 휴리스틱)
    - 가속도 크기(|a|)가 1g(9.81)에서 벗어날수록 위험 증가
    - 자이로 크기(|g|)가 커질수록 위험 증가
    """
    ax = _to_float(sensor.get("ax"), "sensor.ax")
    ay = _to_float(sensor.get("ay"), "sensor.ay")
    az = _to_float(sensor.get("az"), "sensor.az")

    gx = _to_float(sensor.get("gx"), "sensor.gx")
    gy = _to_float(sensor.get("gy"), "sensor.gy")
    gz = _to_float(sensor.get("gz"), "sensor.gz")

    a_mag = math.sqrt(ax**2 + ay**2 + az**2)
    g_mag = math.sqrt(gx**2 + gy**2 + gz**2)

    # 가속도: 1g 기준 편차를 0~1로 정규화(대략)
    acc_dev = abs(a_mag - 9.81)
    acc_score = min(1.0, acc_dev / 6.0)

    # 자이로: 크기를 0~1로 정규화(대략)
    gyro_score = min(1.0, g_mag / 6.0)

    score = 0.7 * acc_score + 0.3 * gyro_score
    return max(0.0, min(1.0, score))



    # ------------------------------------------------------------
    # JSON 파싱
    # ------------------------------------------------------------
    try:
        body = req.get_json()
    except ValueError:
        logging.warning("BadRequest: Invalid JSON")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "Invalid JSON"}),
            status_code=400,
            mimetype="application/json",
        )

    # ------------------------------------------------------------
    # 스키마 고정: timestamp / sensor / isFall
    # ------------------------------------------------------------
    timestamp = body.get("timestamp")
    sensor = body.get("sensor")
    client_is_fall = body.get("isFall")

    # 로그: 들어온 payload 확인 (포털에서 확인용)
    logging.info(f"http_trigger called. timestamp={timestamp}, client_isFall={client_is_fall}")

    if not isinstance(timestamp, str):
        logging.warning("BadRequest: timestamp is not string")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "'timestamp' must be an ISO8601 string"}),
            status_code=400,
            mimetype="application/json",
        )

    if not isinstance(sensor, dict):
        logging.warning("BadRequest: sensor is not object")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "'sensor' must be an object"}),
            status_code=400,
            mimetype="application/json",
        )

    if not isinstance(client_is_fall, bool):
        logging.warning("BadRequest: isFall is not boolean")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "'isFall' must be a boolean"}),
            status_code=400,
            mimetype="application/json",
        )

    # ------------------------------------------------------------
    # 서버 판단 (threshold = 0.7)
    # ------------------------------------------------------------
    try:
        fall_score = _calc_fall_score(sensor)
    except ValueError as e:
        logging.warning(f"BadRequest: {str(e)}")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": str(e)}),
            status_code=400,
            mimetype="application/json",
        )

    server_is_fall = fall_score >= THRESHOLD

    # 로그: 계산 결과 (포털에서 확인용)
    logging.info(f"computed fallScore={fall_score:.4f}, threshold={THRESHOLD}, server_isFall={server_is_fall}")

    # ------------------------------------------------------------
    # 응답
    # ------------------------------------------------------------
    resp = {
        "status": "ok",
        "threshold": THRESHOLD,
        "fallScore": round(fall_score, 4),
        "isFall": server_is_fall,
        "received": {
            "timestamp": timestamp,
            "sensor": sensor,
            "isFall": client_is_fall
        }
    }

    return func.HttpResponse(
        json.dumps(resp),
        status_code=200,
        mimetype="application/json",
    )
'''
import json
import os
import uuid
import datetime
import logging
from datetime import timezone

import azure.functions as func
from azure.storage.blob import BlobServiceClient
import requests

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# =========================
# Blob 설정
# =========================
BLOB_CONNECTION_STRING = os.getenv("BLOB_CONNECTION_STRING")
BLOB_CONTAINER_NAME = os.getenv("BLOB_CONTAINER_NAME")

BLOB_SERVICE_CLIENT = (
    BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
    if BLOB_CONNECTION_STRING
    else None
)

# =========================
# Queue / Teams 설정
# =========================
ALERT_QUEUE_NAME = os.getenv("ALERT_QUEUE_NAME", "auto-reported-alert")
# Azure에 설정된 WORKFLOW_WEBHOOK_URL 기본
WORKFLOW_WEBHOOK_URL = os.getenv("WORKFLOW_WEBHOOK_URL")

# =========================
# HTTP Trigger
# =========================
@app.route(route="http_trigger", methods=["POST"])
def http_trigger(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)

    body["_ingestedAt"] = datetime.datetime.now(timezone.utc).isoformat()

    event_dt = datetime.datetime.fromisoformat(
        body["timestamp"].replace("Z", "+00:00")
    )
    blob_path = f"fall-detection/{event_dt:%Y/%m/%d}/{uuid.uuid4()}.json"

    try:
        container = BLOB_SERVICE_CLIENT.get_container_client(BLOB_CONTAINER_NAME)
        container.upload_blob(
            name=blob_path,
            data=json.dumps(body, ensure_ascii=False),
            overwrite=False,
            content_type="application/json",
        )
    except Exception as e:
        logging.exception("Blob upload failed")
        return func.HttpResponse(str(e), status_code=500)

    # auto_reported만 큐로 보냄
    if body.get("type") == "auto_reported":
        from azure.storage.queue import QueueClient

        queue = QueueClient.from_connection_string(
            os.getenv("AzureWebJobsStorage"),
            ALERT_QUEUE_NAME,
        )

        queue.send_message(json.dumps({
            "eventId": str(uuid.uuid4()),
            "timestamp": body["timestamp"],
            "type": body["type"],
            "device": body.get("device"),
            "blobPath": blob_path,
        }))

        logging.info("Enqueued auto_reported alert")
    else:
        logging.warning(f"[HTTP] skip enqueue. type={body.get('type')} payload={body}")

    return func.HttpResponse(
        json.dumps({"status": "accepted", "blobPath": blob_path}),
        status_code=201,
        mimetype="application/json",
    )

# =========================
# Queue Trigger
# =========================
@app.function_name(name="notify_teams")
@app.queue_trigger(
    arg_name="msg",
    queue_name="%ALERT_QUEUE_NAME%",
    connection="AzureWebJobsStorage",
)
def notify_teams(msg: func.QueueMessage):
    raw = None
    try:
        raw = msg.get_body().decode("utf-8", errors="ignore")
        logging.warning(f"[QUEUE] raw message received (len={len(raw)})")

        try:
            payload = msg.get_json()
        except Exception:
            logging.exception(f"[QUEUE] invalid message. raw={raw}")
            return  # poison 방지: 깨진 메시지는 건너뛴다

        logging.warning(f"[QUEUE] received: {payload}")

        if not WORKFLOW_WEBHOOK_URL:
            logging.error("WORKFLOW_WEBHOOK_URL not set. Skip sending.")
            return  # ❗ 절대 raise 하지 마라

        logging.info(
            f"[QUEUE] posting to workflow url(len)={len(WORKFLOW_WEBHOOK_URL)} "
            f"keys={list(payload.keys())}"
        )

        try:
            res = requests.post(
                WORKFLOW_WEBHOOK_URL,
                json=payload,
                timeout=10,
            )
            # 워크플로 응답을 함께 남겨서 문제 파악 쉽게
            try:
                body = res.text
            except Exception:
                body = "<no body>"
            if res.status_code >= 300:
                logging.error(f"Teams webhook failed status={res.status_code} body={body}")
            else:
                logging.info(f"Teams webhook status={res.status_code} body={body}")
        except Exception as e:
            logging.exception("Teams webhook failed")
            # raise ❌ → poison 방지
    except Exception:
        logging.exception(f"[QUEUE] notify_teams fatal. raw={raw}")
        return  # 모든 예외를 삼켜 poison 방지


# =========================
# Blob Trigger: 업로드된 JSON > 워크플로
# =========================
@app.function_name(name="blob_to_workflow")
@app.blob_trigger(
    arg_name="in_blob",
    path="functoblob-data/fall-detection/{name}",
    connection="AzureWebJobsStorage",
)
def blob_to_workflow(in_blob: func.InputStream):
    raw = in_blob.read().decode("utf-8", errors="ignore")
    try:
        payload = json.loads(raw)
    except Exception:
        logging.exception(f"[BLOB] invalid json. path={in_blob.name}")
        return

    logging.warning(f"[BLOB] received path={in_blob.name} keys={list(payload.keys())}")

    # 오래된 이벤트 무시
    try:
        evt_dt = datetime.datetime.fromisoformat(
            str(payload.get("timestamp")).replace("Z", "+00:00")
        )
        if evt_dt < datetime.datetime.now(timezone.utc) - datetime.timedelta(minutes=15):
            logging.info(f"[BLOB] skip old event. ts={evt_dt.isoformat()} path={in_blob.name}")
            return
    except Exception:
        logging.exception(f"[BLOB] invalid timestamp. path={in_blob.name}")
        return

    if not WORKFLOW_WEBHOOK_URL:
        logging.error("WORKFLOW_WEBHOOK_URL not set. Skip sending.")
        return

    try:
        res = requests.post(
            WORKFLOW_WEBHOOK_URL,
            json=payload,
            timeout=10,
        )
        logging.info(f"[BLOB] workflow status={res.status_code} body={res.text}")
    except Exception:
        logging.exception("[BLOB] workflow call failed")

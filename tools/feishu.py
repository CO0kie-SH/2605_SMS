from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

import requests


class FeishuNotifier:
    """Send small workflow notifications to Feishu webhooks."""

    def __init__(
        self,
        config_file: str | Path = "config/FeiShu.csv",
        logger: logging.Logger | None = None,
        timeout: int = 10,
    ):
        self.config_file = Path(config_file)
        self.logger = logger or logging.getLogger(__name__)
        self.timeout = timeout
        self.configs = self._load_configs()

    def _load_configs(self) -> list[dict[str, str]]:
        if not self.config_file.exists():
            self.logger.warning("飞书配置文件不存在: %s", self.config_file)
            return []

        configs: list[dict[str, str]] = []
        try:
            with self.config_file.open("r", encoding="utf-8", newline="") as file:
                reader = csv.DictReader(file)
                for row in reader:
                    tag = str(row.get("tag") or "").strip()
                    url = str(row.get("url") or "").strip()
                    mode = str(row.get("mode") or "").strip().lower()
                    if tag and url:
                        configs.append({"tag": tag, "url": url, "mode": mode or "text"})
        except Exception as error:
            self.logger.error("加载飞书配置失败: %s", error)
            return []

        self.logger.info("加载飞书配置 %s 条", len(configs))
        return configs

    @staticmethod
    def _build_message(body: str, title: str | None = None, mode: str = "text") -> dict[str, Any]:
        if mode == "text" or not title:
            return {
                "msg_type": "text",
                "content": {"text": body},
            }
        return {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh-CN": {
                        "title": title,
                        "content": [[{"tag": "text", "text": body}]],
                    }
                }
            },
        }

    def send_message(self, body: str, title: str | None = None, tag: str | None = None) -> dict[str, bool]:
        results: dict[str, bool] = {}
        if not self.configs:
            self.logger.warning("无飞书配置，跳过发送")
            return results

        for config in self.configs:
            config_tag = config["tag"]
            config_mode = config["mode"]
            if tag and config_tag != tag:
                continue

            if config_mode == "none":
                results[config_tag] = True
                self.logger.info("飞书机器人 [%s] 模式为 none，跳过发送", config_tag)
                continue

            if config_mode not in {"text", "post", "title"}:
                self.logger.warning("飞书机器人 [%s] 未知模式: %s", config_tag, config_mode)
                results[config_tag] = False
                continue

            message_mode = "text" if config_mode == "text" else "post"
            message = self._build_message(body, title=title, mode=message_mode)
            results[config_tag] = self._send_to_webhook(config["url"], message, config_tag)

        return results

    def _send_to_webhook(self, url: str, message: dict[str, Any], tag: str) -> bool:
        try:
            response = requests.post(url, json=message, timeout=self.timeout)
            if response.status_code != 200:
                self.logger.error("飞书消息发送失败 [%s]，状态码: %s", tag, response.status_code)
                return False
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if payload and payload.get("StatusCode") not in (None, 0):
                self.logger.error("飞书消息发送失败 [%s]: %s", tag, payload)
                return False
            self.logger.info("飞书消息发送成功 [%s]", tag)
            return True
        except requests.RequestException as error:
            self.logger.error("飞书消息发送异常 [%s]: %s", tag, error)
            return False

    def notify_phone_active_presence(self, phone: str, exists: bool, tag: str | None = None) -> dict[str, bool]:
        status_text = "存在于当前激活列表" if exists else "不存在于当前激活列表"
        body = f"号码 {phone} {status_text}"
        return self.send_message(body, title="HeroSMS 号码激活列表确认", tag=tag)

    def notify_sms_code(
        self,
        phone: str,
        sms_code: str,
        sms_text: str = "",
        code_index: int | None = None,
        tag: str | None = None,
    ) -> dict[str, bool]:
        index_text = f"第 {code_index} 次" if code_index is not None else "新"
        body_lines = [
            f"号码：{phone}",
            f"{index_text}验证码：{sms_code}",
        ]
        if sms_text:
            body_lines.append(f"短信内容：{sms_text}")
        return self.send_message("\n".join(body_lines), title="HeroSMS 收到验证码", tag=tag)

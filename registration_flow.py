# -*- coding: utf-8 -*-
"""Shared registration workflow used by both GUI and CLI."""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Type


def _noop(*_args, **_kwargs):
    return None


@dataclass
class RegistrationCallbacks:
    log: Callable[[str], None]
    cancelled: Callable[[], bool]


@dataclass
class RegistrationObserver:
    on_stats: Callable[[int, int], None] = _noop
    on_result: Callable[["RegistrationResult", "OutputResult"], None] = _noop


@dataclass
class RegistrationOperations:
    start_browser: Callable[[Callable[[str], None]], None]
    browser_missing: Callable[[], bool]
    restart_browser: Callable[[Callable[[str], None]], None]
    cleanup_runtime_memory: Callable[[Callable[[str], None], str], None]
    sleep_with_cancel: Callable[[float, Callable[[], bool]], None]
    open_signup_page: Callable[[Callable[[str], None], Callable[[], bool]], None]
    fill_email_and_submit: Callable[[Callable[[str], None], Callable[[], bool]], Any]
    save_mail_credential: Callable[[str, str], Dict[str, Any]]
    fill_code_and_submit: Callable[[str, str, Callable[[str], None], Callable[[], bool]], str]
    fill_profile_and_submit: Callable[[Callable[[str], None], Callable[[], bool]], Dict[str, Any]]
    wait_for_sso_cookie: Callable[[Callable[[str], None], Callable[[], bool]], str]
    enable_nsfw_for_token: Callable[[str, Callable[[str], None]], Any]
    save_account_result: Callable[["RegistrationResult", str], Dict[str, Any]]
    add_token_pools: Callable[["RegistrationResult", Callable[[str], None]], Dict[str, Any]]
    export_cpa: Callable[["RegistrationResult", Callable[[str], None], Callable[[], bool]], Dict[str, Any]]
    cancelled_error: Type[BaseException]
    retry_error: Type[BaseException]


@dataclass
class RegistrationResult:
    ok: bool
    email: str = ""
    password: str = ""
    sso: str = ""
    profile: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    retryable: bool = False


@dataclass
class OutputContext:
    accounts_output_file: str
    save_attempts: int = 3


@dataclass
class OutputResult:
    registered: bool
    saved: bool
    save_attempts: int = 0
    save_error: str = ""
    token_pools: Dict[str, Any] = field(default_factory=dict)
    cpa: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchResult:
    requested: int
    success_count: int = 0
    fail_count: int = 0
    cancelled: bool = False
    results: List[RegistrationResult] = field(default_factory=list)
    outputs: List[OutputResult] = field(default_factory=list)
    pending_outputs: List[RegistrationResult] = field(default_factory=list)


def register_one_account(
    callbacks: RegistrationCallbacks,
    operations: RegistrationOperations,
    enable_nsfw: bool = True,
    max_mail_retry: int = 3,
) -> RegistrationResult:
    email = ""
    dev_token = ""
    code = ""
    for mail_try in range(1, max_mail_retry + 1):
        callbacks.log(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
        operations.open_signup_page(callbacks.log, callbacks.cancelled)
        callbacks.log("[*] 2. 创建邮箱并提交")
        email, dev_token = operations.fill_email_and_submit(
            callbacks.log, callbacks.cancelled
        )
        callbacks.log(f"[*] 邮箱: {email}")
        callbacks.log(f"[Debug] 邮箱credential(jwt): {dev_token}")
        credential_status = operations.save_mail_credential(email, dev_token)
        if not credential_status.get("ok"):
            callbacks.log(
                "[!] 邮箱凭据保存失败，注册流程继续但已记录警告: "
                + str(credential_status.get("error") or "unknown error")
            )
        callbacks.log("[*] 3. 拉取验证码")
        try:
            code = operations.fill_code_and_submit(
                email, dev_token, callbacks.log, callbacks.cancelled
            )
            break
        except operations.cancelled_error:
            raise
        except Exception as exc:
            message = str(exc)
            if (
                ("未收到验证码" in message or "验证码" in message)
                and mail_try < max_mail_retry
            ):
                callbacks.log(
                    f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {message}"
                )
                operations.restart_browser(callbacks.log)
                operations.sleep_with_cancel(1, callbacks.cancelled)
                continue
            raise
    else:
        raise RuntimeError("验证码阶段失败，已达到最大重试次数")

    callbacks.log(f"[*] 验证码: {code}")
    callbacks.log("[*] 4. 填写资料")
    profile = operations.fill_profile_and_submit(
        callbacks.log, callbacks.cancelled
    )
    callbacks.log(
        f"[*] 资料已填: {profile.get('given_name', '')} "
        f"{profile.get('family_name', '')}"
    )
    callbacks.log("[*] 5. 等待 sso cookie")
    sso = operations.wait_for_sso_cookie(callbacks.log, callbacks.cancelled)
    if enable_nsfw:
        callbacks.log("[*] 6. 开启 NSFW")
        nsfw_ok, nsfw_message = operations.enable_nsfw_for_token(
            sso, callbacks.log
        )
        if nsfw_ok:
            callbacks.log(f"[+] NSFW 开启成功: {nsfw_message}")
        else:
            callbacks.log(f"[!] NSFW 未开启，继续保存账号: {nsfw_message}")
    return RegistrationResult(
        ok=True,
        email=email,
        password=str(profile.get("password") or ""),
        sso=sso,
        profile=profile,
    )


def persist_account_result(
    result: RegistrationResult,
    context: OutputContext,
    callbacks: RegistrationCallbacks,
    operations: RegistrationOperations,
) -> OutputResult:
    attempts = max(int(context.save_attempts), 1)
    save_error = ""
    saved = False
    used_attempts = 0
    for attempt in range(1, attempts + 1):
        used_attempts = attempt
        status = operations.save_account_result(
            result, context.accounts_output_file
        )
        if status.get("ok"):
            saved = True
            break
        save_error = str(status.get("error") or "unknown error")
        callbacks.log(
            f"[!] 保存账号结果失败 ({attempt}/{attempts}): {save_error}"
        )
        if attempt < attempts:
            operations.sleep_with_cancel(
                min(0.5 * attempt, 1.5), callbacks.cancelled
            )

    if not saved:
        return OutputResult(
            registered=True,
            saved=False,
            save_attempts=used_attempts,
            save_error=save_error,
        )

    token_pools = operations.add_token_pools(result, callbacks.log)
    for target in ("local", "remote"):
        target_result = token_pools.get(target) or {}
        if target_result.get("enabled") and not target_result.get("ok"):
            callbacks.log(
                f"[!] grok2api {target} 入池失败，账号已安全保存: "
                f"{target_result.get('error') or 'unknown error'}"
            )

    cpa = operations.export_cpa(
        result, callbacks.log, callbacks.cancelled
    )
    return OutputResult(
        registered=True,
        saved=True,
        save_attempts=used_attempts,
        token_pools=token_pools,
        cpa=cpa,
    )


def run_batch(
    count: int,
    callbacks: RegistrationCallbacks,
    observer: RegistrationObserver,
    operations: RegistrationOperations,
    output_context: OutputContext,
    enable_nsfw: bool = True,
    cleanup_interval: int = 5,
    max_slot_retry: int = 3,
) -> BatchResult:
    batch = BatchResult(requested=count)
    retry_count_for_slot = 0
    index = 0
    operations.start_browser(callbacks.log)
    callbacks.log("[*] 浏览器已启动")
    try:
        while index < count:
            if callbacks.cancelled():
                batch.cancelled = True
                break
            callbacks.log(f"--- 开始第 {index + 1}/{count} 个账号 ---")
            try:
                registration = register_one_account(
                    callbacks,
                    operations,
                    enable_nsfw=enable_nsfw,
                )
                output = persist_account_result(
                    registration,
                    output_context,
                    callbacks,
                    operations,
                )
                batch.results.append(registration)
                batch.outputs.append(output)
                observer.on_result(registration, output)
                retry_count_for_slot = 0
                index += 1
                if output.saved:
                    batch.success_count += 1
                    callbacks.log(
                        f"[+] 注册并保存成功: {registration.email}"
                    )
                    if (
                        cleanup_interval > 0
                        and batch.success_count % cleanup_interval == 0
                        and index < count
                    ):
                        operations.cleanup_runtime_memory(
                            callbacks.log,
                            f"已成功 {batch.success_count} 个账号，执行定期清理",
                        )
                else:
                    batch.fail_count += 1
                    batch.pending_outputs.append(registration)
                    callbacks.log(
                        "[-] 账号已注册但结果未能持久化，未计入成功并加入"
                        f"待重试队列: {registration.email}: "
                        f"{output.save_error}"
                    )
            except operations.cancelled_error:
                batch.cancelled = True
                callbacks.log("[!] 注册被停止")
                break
            except operations.retry_error as exc:
                retry_count_for_slot += 1
                if retry_count_for_slot <= max_slot_retry:
                    callbacks.log(
                        "[!] 当前账号流程卡住，重试第 "
                        f"{retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                    )
                else:
                    batch.fail_count += 1
                    retry_count_for_slot = 0
                    index += 1
                    callbacks.log(
                        f"[-] 当前账号已达到最大重试次数，跳过: {exc}"
                    )
            except Exception as exc:
                batch.fail_count += 1
                retry_count_for_slot = 0
                index += 1
                callbacks.log(f"[-] 注册失败: {exc}")
            finally:
                observer.on_stats(
                    batch.success_count, batch.fail_count
                )
                if callbacks.cancelled():
                    batch.cancelled = True
                    break
                if operations.browser_missing():
                    operations.start_browser(callbacks.log)
                else:
                    operations.restart_browser(callbacks.log)
                operations.sleep_with_cancel(1, callbacks.cancelled)
    finally:
        operations.cleanup_runtime_memory(callbacks.log, "任务结束")
    return batch

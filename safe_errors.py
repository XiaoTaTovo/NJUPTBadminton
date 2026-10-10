"""Structured local failures. Arbitrary exception messages are never telemetry."""
import math
MESSAGES = {
 'BOUNDARY_RETRY_EXPIRED':'开放边界重试已超时，未发送请求。',
 'SAFE_STOP':'安全检查未通过，任务停止。',
 'CLOCK_COMMAND_TIMEOUT':'校时命令超时。',
 'CLOCK_SAMPLES_INSUFFICIENT':'无法取得至少两个有效校时样本。',
 'CLOCK_SAMPLE_SPREAD_HIGH':'校时样本抖动超过250ms。',
 'CLOCK_OFFSET_OUT_OF_RANGE':'校时偏差超过60秒。',
 'CLOCK_REFRESH_EXHAUSTED':'临近校时未能在期限内成功；不使用旧时钟继续。',
 'WAIT_DISCONTINUITY':'等待出现异常调度空档或时钟关系变化。',
 'PREPARE_DEADLINE_MISSED':'准备时间不足或已错过准备窗口。',
 'CREDENTIAL_COVERAGE_INSUFFICIENT':'凭据无效或无法覆盖本次预约窗口，请刷新。',
 'AUTH_REJECTED':'只读请求的认证被拒绝，请刷新凭据。',
 'SLOTS_QUERY_FAILED':'场次读取失败或响应结构异常。',
 'GATEWAY_TIME_CONFLICT':'网关时间旁证缺失、无效或与参考时间冲突。',
 'USER_CANCELLED':'用户已中止任务。',
 'INTERNAL_ERROR':'程序内部异常，请查看脱敏诊断。',
 'LOG_WRITE_FAILED':'诊断或提交意图无法可靠保存，停止新增提交。',
 'TASK_ALREADY_RUNNING':'已有任务正在运行，请使用已有窗口。',
 'PREVIOUS_TASK_UNKNOWN':'上次任务状态未确认，请先人工核对订单。',
}
NUMERIC_FIELDS = {'round','sample_count','spread_seconds','offset_seconds','elapsed_ms','return_code',
 'remaining_seconds','retry_in_seconds','clock_age_seconds','http_status','gap_seconds','jump_seconds',
 'post_count','slots_get_count','types_get_count','success_count','credited_count','unknown_count'}
BOOL_FIELDS = {'gateway_corroborated','telemetry_incomplete','wake_requested','request_may_have_started'}

def safe_details(details):
    result={}
    for key,value in (details or {}).items():
        if key in NUMERIC_FIELDS and type(value) in (int,float) and math.isfinite(value): result[key]=value
        elif key in BOOL_FIELDS and type(value) is bool: result[key]=value
        elif key=='source' and value in ('time.windows.com','time.cloudflare.com'): result[key]=value
        elif key=='last_error_code' and value in MESSAGES: result[key]=value
    return result

class SafeError(RuntimeError):
    def __init__(self, message=None, *, code='SAFE_STOP', safe_message=None, details=None):
        self.code=code if code in MESSAGES else 'INTERNAL_ERROR'
        # Only registry text is eligible for persistence. Legacy messages remain console-only.
        self.safe_message=MESSAGES[self.code]
        self.details=safe_details(details)
        super().__init__(message if message is not None else self.safe_message)

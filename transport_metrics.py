"""Requests adapter telemetry: no headers, URLs, payloads or credential values retained."""
import time
from requests.adapters import HTTPAdapter

class MeasuredAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_connection=None
        self._last_socket=None

    def send(self, request, **kwargs):
        trace=getattr(request,'_booking_trace',None)
        if trace is None:trace={}
        trace['transport_enter_ns']=time.perf_counter_ns()
        response=super().send(request,**kwargs)
        trace['response_headers_ns']=time.perf_counter_ns()
        # Observe GETs too: first POST may reuse the earlier preparation query's socket.
        conn=getattr(response.raw,'_connection',None)
        sock=getattr(conn,'sock',None)
        trace['connection_reuse_observed']=(conn is self._last_connection and sock is self._last_socket) if conn is not None and sock is not None else None
        if conn is not None and sock is not None:self._last_connection=conn;self._last_socket=sock
        response._booking_transport_trace=trace
        return response

    def close(self):
        self._last_connection=None;self._last_socket=None
        super().close()

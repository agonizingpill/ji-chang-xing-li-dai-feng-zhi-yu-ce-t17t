import json, os, time
from pathlib import Path
import redis
from .db import connect, migrate, wait_for_db
from .algorithm import stl_rolling_forecast
from . import mobile_dispatch_service as mobile_service

REDIS_URL=os.environ['REDIS_URL']; QUEUE='airport:forecast'
r=redis.Redis.from_url(REDIS_URL, decode_responses=True)

def trigger_mobile_recompute(wave_id):
    """上游预测更新后，按新缺口重算该航站楼机动派工；失败不影响预测主流程。"""
    try:
        with connect() as conn:
            terminal = conn.execute(
                'SELECT terminal FROM flight_waves WHERE id=%s', (wave_id,)).fetchone()['terminal']
            mobile_service.ensure_schema(conn)
            mobile_service.recompute(conn, terminal, None, trigger='forecast_update')
            conn.commit()
    except Exception as exc:
        print('mobile dispatch recompute failed:', exc)

def process(forecast_id):
    with connect() as conn:
        row=conn.execute('SELECT * FROM peak_forecasts WHERE id=%s FOR UPDATE',(forecast_id,)).fetchone()
        if not row or row['status'] not in ('queued','failed'): return
        conn.execute("UPDATE peak_forecasts SET status='running', attempts=attempts+1, updated_at=now() WHERE id=%s", (forecast_id,))
        wave=conn.execute('SELECT * FROM flight_waves WHERE id=%s',(row['wave_id'],)).fetchone()
        points=conn.execute("SELECT counted_at,bag_count FROM belt_counts WHERE wave_id=%s AND status='accepted' ORDER BY counted_at", (row['wave_id'],)).fetchall()
        succeeded=False
        try:
            result=stl_rolling_forecast(points, row['window_size'])
            conn.execute("UPDATE peak_forecasts SET status='ready', peak_bags=%s, forecast_at=now(), seasonal_strength=%s, trend_per_hour=%s, snapshot_hash=%s, parameters=%s::jsonb, result=%s::jsonb, updated_at=now() WHERE id=%s", (result['peak_bags'],result['seasonal_strength'],result['trend_per_hour'],result['snapshot_hash'],json.dumps({'method':'STL decomposition + rolling linear regression','seed':1072,'period':result['period']}),json.dumps(result),forecast_id))
            conn.execute("UPDATE flight_waves SET status='forecasted' WHERE id=%s", (row['wave_id'],))
            succeeded=True
        except Exception as exc:
            conn.execute("UPDATE peak_forecasts SET status='failed', failure_reason=%s, updated_at=now() WHERE id=%s", (str(exc), forecast_id))
        conn.commit()
    if succeeded:
        trigger_mobile_recompute(row['wave_id'])

def main():
    wait_for_db(); migrate(); Path('/shared/worker-heartbeat').touch()
    while True:
        Path('/shared/worker-heartbeat').touch()
        item=r.blpop(QUEUE, timeout=3)
        if item:
            try: process(int(item[1]))
            except Exception: time.sleep(1)

if __name__=='__main__': main()

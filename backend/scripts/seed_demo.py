import json
from datetime import datetime, timedelta, timezone
from src.db import connect
from src.auth import password_hash
from src import mobile_dispatch_service as mobile_service

def main():
    base=datetime(2026,11,21,6,0,tzinfo=timezone.utc)
    with connect() as conn:
        for username,display,role in [('ops.demo','值机调度员','operator'),('manager.demo','值班经理','supervisor'),('audit.demo','运行观察员','viewer')]:
            conn.execute('INSERT INTO users(username,password_hash,display_name,role) VALUES (%s,%s,%s,%s) ON CONFLICT(username) DO UPDATE SET display_name=EXCLUDED.display_name, role=EXCLUDED.role', (username,password_hash('Airport1072!'),display,role))
        op=conn.execute("SELECT id FROM users WHERE username='ops.demo'").fetchone()['id']
        waves=[('WAVE-AM-0615','T2',base,base+timedelta(hours=2),8,420,'normal','早高峰国际到达'),('WAVE-PM-1810','T3',base+timedelta(hours=12),base+timedelta(hours=14),6,300,'normal','晚高峰转机波次'),('WAVE-CANCEL-0900','T1',base+timedelta(hours=3),base+timedelta(hours=4),3,140,'cancelled_unmarked','航班取消但未完成运行标记'),('WAVE-AM-0645','T2',base+timedelta(minutes=30),base+timedelta(hours=2,minutes=30),5,260,'normal','与早高峰撞峰的相邻 T2 波次')]
        for code,terminal,start,end,flights,bags,cancel,notes in waves:
            conn.execute('INSERT INTO flight_waves(code,terminal,arrival_start,arrival_end,flight_count,planned_bags,cancellation_state,notes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(code) DO NOTHING',(code,terminal,start,end,flights,bags,cancel,notes))
        wave_ids={r['code']:r['id'] for r in conn.execute('SELECT id,code FROM flight_waves').fetchall()}
        for code, wave_code, start, vals in [
            ('BAG-AM','WAVE-AM-0615',base,[42,55,68,80,96,115,132,145,158,176,189,205,220,238,252,268]),
            ('BAG-PM','WAVE-PM-1810',base+timedelta(hours=12),[35,44,58,65,74,88,102,120,138,151,166,180]),
            ('BAG-CANCEL','WAVE-CANCEL-0900',base+timedelta(hours=3),[20,25,30,35,40,45,50,55]),
            ('BAG-AM2','WAVE-AM-0645',base+timedelta(minutes=30),[38,47,60,72,85,98,112,126])]:
            wid=wave_ids[wave_code]
            for i,value in enumerate(vals):
                conn.execute('INSERT INTO belt_counts(wave_id,belt_code,counted_at,bag_count,scan_source) VALUES (%s,%s,%s,%s,%s) ON CONFLICT(wave_id,belt_code,counted_at) DO NOTHING',(wid,code,start+timedelta(minutes=10*i),value,'belt-counter-07'))
        conn.execute("INSERT INTO staff_shifts(shift_code,wave_id,start_at,end_at,planned_staff,capacity,status) VALUES ('SHIFT-AM-A',%s,%s,%s,18,20,'draft') ON CONFLICT(shift_code) DO NOTHING",(wave_ids['WAVE-AM-0615'],base+timedelta(minutes=30),base+timedelta(hours=2,minutes=30)))
        conn.execute("INSERT INTO staff_shifts(shift_code,wave_id,start_at,end_at,planned_staff,capacity,status) VALUES ('SHIFT-PM-C',%s,%s,%s,8,10,'draft') ON CONFLICT(shift_code) DO NOTHING",(wave_ids['WAVE-PM-1810'],base+timedelta(hours=12),base+timedelta(hours=15)))

        # 就绪预测（含未来 4 个十分钟档件数），驱动机动班组缺口看板。
        # 两波的预测档在 08:30/08:40 真正撞峰，演示同档班组竞争与“同组同档只派一处”。
        seeded_forecasts=[
            ('WAVE-AM-0615', base+timedelta(minutes=140), 185.0, [150,170,185,172]),
            ('WAVE-AM-0645', base+timedelta(minutes=110), 150.0, [110,132,150,140]),
            ('WAVE-PM-1810', base+timedelta(hours=13,minutes=50), 120.0, [95,110,120,108]),
        ]
        for wave_code, forecast_at, peak, predicted in seeded_forecasts:
            wid=wave_ids[wave_code]
            exists=conn.execute("SELECT 1 FROM peak_forecasts WHERE wave_id=%s AND status IN ('ready','published')",(wid,)).fetchone()
            if exists:
                continue
            result={'peak_bags':peak,'predicted_next_4':predicted,'seasonal_strength':0.62,
                    'trend_per_hour':4.5,'window_size':8,'period':8,'snapshot_hash':'seed-demo',
                    'input_points':8}
            conn.execute("""INSERT INTO peak_forecasts(wave_id,status,peak_bags,forecast_at,
                seasonal_strength,trend_per_hour,window_size,snapshot_hash,parameters,result,created_by)
                VALUES (%s,'ready',%s,%s,0.62,4.5,8,'seed-demo','{}'::jsonb,%s::jsonb,%s)""",
                (wid,peak,forecast_at,json.dumps(result),op))
            conn.execute("UPDATE flight_waves SET status='forecasted' WHERE id=%s",(wid,))

        # 机动班组台账（同航站楼撞峰波次共享）
        mobile_service.ensure_schema(conn)
        teams=[('FLEX-T2-A','T2',60),('FLEX-T2-B','T2',45),('FLEX-T2-C','T2',30),('FLEX-T3-A','T3',50)]
        for code,terminal,capacity in teams:
            conn.execute('INSERT INTO mobile_teams(team_code,terminal,capacity) VALUES (%s,%s,%s) ON CONFLICT(team_code) DO NOTHING',(code,terminal,capacity))

        # 初始重排：把三个 T2 机动班组按各档缺口派给撞峰的两个 T2 波次
        seeded=conn.execute("SELECT 1 FROM mobile_dispatch_runs WHERE terminal='T2' LIMIT 1").fetchone()
        if not seeded:
            manager=conn.execute("SELECT id FROM users WHERE username='manager.demo'").fetchone()['id']
            class _Actor:
                def __init__(self, actor_id): self.id=actor_id
            mobile_service.recompute(conn,'T2',_Actor(manager),trigger='manual')
        conn.commit()
if __name__=='__main__': main()

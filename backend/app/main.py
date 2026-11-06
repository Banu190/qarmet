"""Qarmet corporate taxi MVP API. JSON storage is intentionally isolated here."""
from __future__ import annotations
import json, os, threading, uuid, asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Header, Depends, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator
from .services.assignment import VehicleAssignmentService, location_is_fresh, vehicle_position
import csv, io, zipfile, urllib.parse, urllib.request, urllib.error, time
from datetime import date
from zoneinfo import ZoneInfo
from fastapi.responses import StreamingResponse
from fastapi import Query
from .services.reporting import filtered_orders, normalized_datasets, xlsx_bytes, csv_zip_bytes, REPORT_TZ, to_local_datetime, status_label

DATA = Path(os.getenv('QARMET_DATA', Path(__file__).resolve().parents[1] / 'data.json'))
LOCK = threading.RLock()
MUTATION_LOCK = asyncio.Lock()
ROLES = ('dispatcher','driver','employee')
NAMES = {'dispatcher':'Айгуль Сарсенова','driver':'Ерлан Нурланов','employee':'Данияр Ахметов'}
VEHICLE = {'id':'car-1','make':'Toyota','model':'Camry','plate':'123 ABC 09','class':'Стандарт','capacity':4,'driver_id':'driver-1','status':'offline','city':'Караганда'}
DRIVER_DEMOS=[
    ('driver-1','Ерлан Нурланов'),('driver-2','Марат Ахметов'),('driver-3','Серик Беков'),('driver-4','Азамат Садыков'),('driver-5','Нурлан Омаров'),
    ('driver-6','Данияр Ибраев'),('driver-7','Руслан Касымов'),('driver-8','Тимур Абдрахманов'),('driver-9','Арман Жумабаев'),('driver-10','Бауыржан Сеитов'),
    ('driver-11','Канат Мусин'),('driver-12','Ермек Тлеубаев'),('driver-13','Даурен Кенжебаев'),('driver-14','Алибек Нургалиев'),('driver-15','Самат Куанышев')]
VEHICLE_DEMOS=[
    ('car-1','Toyota','Camry','123 ABC 09','Стандарт',4),('vehicle-02','Hyundai','Sonata','456 ACD 09','Комфорт',4),
    ('vehicle-03','Kia','K5','789 AEF 09','Комфорт',4),('vehicle-04','Toyota','Corolla','234 AGH 09','Стандарт',4),
    ('vehicle-05','Skoda','Octavia','567 AJK 09','Стандарт',4),('vehicle-06','Hyundai','Staria','890 ALM 09','Минивэн',7),
    ('vehicle-07','Toyota','Hiace','345 ANP 09','Минивэн',8),('vehicle-08','Kia','Carnival','678 ARS 09','Минивэн',7),
    ('vehicle-09','Volkswagen','Passat','901 ATU 09','Комфорт',4),('vehicle-10','Toyota','Camry','112 AVX 09','Стандарт',4),
    ('vehicle-11','Hyundai','Elantra','223 AWY 09','Стандарт',4),('vehicle-12','Chevrolet','Malibu','334 AXZ 09','Комфорт',4),
    ('vehicle-13','Toyota','RAV4','445 ABL 09','Комфорт',4),('vehicle-14','Kia','Sportage','556 ACM 09','Комфорт',4),
    ('vehicle-15','Toyota','Land Cruiser Prado','667 ADN 09','Комфорт',4)]
DEMO_POSITIONS=[(49.80470,73.10940),(49.81720,73.08710),(49.79420,73.12140),(49.82610,73.11920),(49.78640,73.09450),(49.81130,73.15120),(49.77680,73.13760),(49.83340,73.07220),(49.80160,73.06080),(49.76890,73.10530),(49.84210,73.13050),(49.78910,73.15820),(49.81930,73.04570),(49.75880,73.08040),(49.85020,73.09710)]
LOCATIONS=['Офис Qarmet','Караганда, проспект Бухар-Жырау','Железнодорожный вокзал','Аэропорт Сары-Арка','Металлургический комбинат','Шахта Костенко','Центральная проходная']
STATES=['Создана','Ожидает обработки','Назначена машина','Водитель принял заказ','Водитель в пути','Водитель на месте','Поездка началась','Завершена','Отменена']
ACTIVE=STATES[2:7]
GEOCODER_BASE=os.getenv('QARMET_NOMINATIM_URL','https://nominatim.openstreetmap.org').rstrip('/')
GEOCODER_LOCK=threading.Lock(); GEOCODER_LAST_REQUEST=0.0; GEOCODER_CACHE={}

def now(): return datetime.now(timezone.utc).isoformat()
def initial():
    profile={
        'dispatcher':{'employee_number':'QRM-DSP-001','department':'Диспетчерская служба'},
        'driver':{'employee_number':'QRM-DRV-001','department':'Автотранспортный участок'},
        'employee':{'employee_number':'QRM-EMP-001','department':'Производственный персонал'},
    }
    users=[{'id':f'{r}-1','name':NAMES[r],'role':r,**profile[r]} for r in ROLES]
    d={'users':users,'vehicles':[VEHICLE.copy()],'shifts':{'driver-1':{'status':'off','updated_at':now()}},'orders':[],'events':[],'notifications':[],'incidents':[],'sessions':{},'locations':LOCATIONS}
    ensure_fleet(d)
    return d
def ensure_fleet(d):
    """Idempotently add demo fleet records while keeping every existing record."""
    changed=False
    users={u['id']:u for u in d.setdefault('users',[])}
    vehicles={v['id']:v for v in d.setdefault('vehicles',[])}
    shifts=d.setdefault('shifts',{})
    settings=d.setdefault('settings',{})
    if settings.get('dispatch_mode') not in ('automatic','manual'): settings['dispatch_mode']='manual'; changed=True
    if 'demo_auto_approval' not in settings: settings['demo_auto_approval']=True; changed=True
    for index,(driver_id,name) in enumerate(DRIVER_DEMOS,1):
        vehicle_id=VEHICLE_DEMOS[index-1][0]
        driver=users.get(driver_id)
        if driver is None:
            driver={'id':driver_id,'name':name,'role':'driver'}; d['users'].append(driver); users[driver_id]=driver; changed=True
        defaults={'employee_number':f'QRM-DRV-{index:03d}','department':'Автотранспортный участок','vehicle_id':vehicle_id,'assigned_vehicle_id':vehicle_id,'phone':f'+7 700 000 {index:02d} {index:02d}','email':f'driver{index:02d}@example.test','availability':'offline','current_order_id':None}
        for key,value in defaults.items():
            if key not in driver: driver[key]=value; changed=True
        if driver.get('role')!='driver': driver['role']='driver'; changed=True
        if driver_id not in shifts: shifts[driver_id]={'status':'off','updated_at':now()}; changed=True
    legacy_status={'Свободен':'available','Занят':'busy','Назначен':'assigned','Перерыв':'break','Офлайн':'offline','На обслуживании':'maintenance'}
    for vehicle_id,make,model,plate,vehicle_class,capacity in VEHICLE_DEMOS:
        index=next(i for i,item in enumerate(VEHICLE_DEMOS) if item[0]==vehicle_id)
        assigned_driver=DRIVER_DEMOS[index][0]
        vehicle=vehicles.get(vehicle_id)
        if vehicle is None:
            vehicle={'id':vehicle_id,'make':make,'model':model,'plate':plate,'class':vehicle_class,'capacity':capacity,'driver_id':assigned_driver,'status':'offline','city':'Караганда'}
            d['vehicles'].append(vehicle); vehicles[vehicle_id]=vehicle; changed=True
        defaults={'make':make,'model':model,'plate':plate,'license_plate':plate,'class':vehicle_class,'capacity':capacity,'driver_id':assigned_driver,'status':'offline','city':'Караганда','garage':'Гараж Qarmet, Караганда','current_latitude':None,'current_longitude':None,'location_updated_at':None,'active_order_id':None}
        for key,value in defaults.items():
            if key not in vehicle: vehicle[key]=value; changed=True
        demo_lat,demo_lon=DEMO_POSITIONS[index]
        for key,value in [('demo_latitude',demo_lat),('demo_longitude',demo_lon)]:
            if vehicle.get(key)!=value: vehicle[key]=value; changed=True
        if not vehicle.get('demo_location_updated_at'): vehicle['demo_location_updated_at']=now(); changed=True
        normalized=legacy_status.get(vehicle.get('status'),vehicle.get('status','offline'))
        if normalized!=vehicle.get('status'): vehicle['status']=normalized; changed=True
        if vehicle.get('license_plate')!=vehicle.get('plate'): vehicle['license_plate']=vehicle.get('plate'); changed=True
        if not vehicle.get('make') and vehicle.get('model','').startswith('Toyota '):
            vehicle['make']='Toyota'; vehicle['model']=vehicle['model'][7:]; changed=True
        old_location=vehicle.get('current_location')
        if isinstance(old_location,dict) and old_location.get('source')=='browser_gps':
            if vehicle.get('current_latitude') is None and old_location.get('latitude') is not None: vehicle['current_latitude']=old_location['latitude']; changed=True
            if vehicle.get('current_longitude') is None and old_location.get('longitude') is not None: vehicle['current_longitude']=old_location['longitude']; changed=True
            if vehicle.get('location_updated_at') is None and old_location.get('updated_at'): vehicle['location_updated_at']=old_location['updated_at']; changed=True
            if vehicle.get('location_source')!='browser_gps': vehicle['location_source']='browser_gps'; changed=True
        if vehicle.get('location_source') not in ('browser_gps','gps'):
            if vehicle.get('current_latitude')!=demo_lat: vehicle['current_latitude']=demo_lat; changed=True
            if vehicle.get('current_longitude')!=demo_lon: vehicle['current_longitude']=demo_lon; changed=True
            if vehicle.get('location_source')!='demo': vehicle['location_source']='demo'; changed=True
            if vehicle.get('location_updated_at')!=vehicle.get('demo_location_updated_at'): vehicle['location_updated_at']=vehicle.get('demo_location_updated_at'); changed=True
        driver=users.get(vehicle['driver_id'])
        if driver and driver.get('assigned_vehicle_id')!=vehicle_id and not driver.get('assigned_vehicle_id'):
            driver['assigned_vehicle_id']=vehicle_id; changed=True
    for vehicle in d['vehicles']:
        assigned=next((o for o in d.get('orders',[]) if o.get('driver_id')==vehicle.get('driver_id') and o.get('status') in ACTIVE),None)
        if assigned:
            if not assigned.get('vehicle_id'): assigned['vehicle_id']=vehicle['id']; changed=True
            if not vehicle.get('active_order_id'): vehicle['active_order_id']=assigned['id']; changed=True
            expected='busy' if assigned['status']=='Поездка началась' else 'assigned'
            if vehicle.get('status')!=expected: vehicle['status']=expected; changed=True
            driver=users.get(vehicle['driver_id'])
            if driver:
                if driver.get('current_order_id')!=assigned['id']: driver['current_order_id']=assigned['id']; changed=True
                availability='busy' if assigned['status']=='Поездка началась' else 'assigned'
                if driver.get('availability')!=availability: driver['availability']=availability; changed=True
        else:
            driver=users.get(vehicle.get('driver_id'))
            if driver:
                availability='active' if shifts.get(driver['id'],{}).get('status')=='active' else 'break' if shifts.get(driver['id'],{}).get('status')=='break' else 'offline'
                if driver.get('current_order_id') is not None: driver['current_order_id']=None; changed=True
                if driver.get('availability')!=availability: driver['availability']=availability; changed=True
    return changed
def ensure_profile(d):
    """Backfill profile fields in existing JSON stores without resetting user state."""
    profile={
        'dispatcher':{'employee_number':'QRM-DSP-001','department':'Диспетчерская служба'},
        'driver':{'employee_number':'QRM-DRV-001','department':'Автотранспортный участок'},
        'employee':{'employee_number':'QRM-EMP-001','department':'Производственный персонал'},
    }
    changed=False
    for user in d.get('users',[]):
        for key,value in profile.get(user.get('role'),{}).items():
            if not user.get(key): user[key]=value; changed=True
    return changed
def ensure_notifications(d):
    if 'notifications' not in d:
        d['notifications']=[]
        return True
    return False
REPORT_TIMESTAMPS=('assigned_at','accepted_at','driver_departed_at','driver_arrived_at','trip_started_at','trip_completed_at','cancelled_at')
def parse_iso(value):
    if not value: return None
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError,AttributeError): return None
def ensure_order_schema(d):
    changed=False; users={u['id']:u for u in d.get('users',[])}; vehicles={v['id']:v for v in d.get('vehicles',[])}
    timestamp_by_status={'Назначена машина':'assigned_at','Водитель принял заказ':'accepted_at','Водитель в пути':'driver_departed_at','Водитель на месте':'driver_arrived_at','Поездка началась':'trip_started_at','Завершена':'trip_completed_at','Отменена':'cancelled_at'}
    for o in d.get('orders',[]):
        driver=users.get(o.get('driver_id'),{}); vehicle=vehicles.get(o.get('vehicle_id'),{}); employee=users.get(o.get('employee_id'),{})
        creator=next((e.get('actor_id') for e in d.get('events',[]) if e.get('order_id')==o.get('id') and e.get('type')=='order_created'),None)
        values={'employee_department':employee.get('department'),'driver_name':driver.get('name'),'vehicle_plate':vehicle.get('plate'),'vehicle_model':vehicle.get('model'),'vehicle_class':vehicle.get('class'),
                'pickup_address':o.get('pickup'),'pickup_latitude':None,'pickup_longitude':None,'destination_address':o.get('destination'),'destination_latitude':None,'destination_longitude':None,
                'passenger_count':o.get('passengers',1),'trip_purpose':o.get('purpose',''),'created_by_user_id':None,'assigned_at':None,'accepted_at':None,'driver_departed_at':None,'driver_arrived_at':None,'trip_started_at':None,'trip_completed_at':None,'cancelled_at':None,'cancellation_reason':o.get('cancel_reason'),
                'trip_status':o.get('status'),'actual_distance_km':None,'estimated_distance_km':None,'trip_duration_minutes':None,'waiting_duration_minutes':None,'cost_center':None}
        if creator: values['created_by_user_id']=creator
        # Legacy coordinates/current labels are preserved when present.
        for key in ('pickup_latitude','pickup_longitude','destination_latitude','destination_longitude','vehicle_plate','vehicle_model','vehicle_class','driver_name','employee_department','cost_center'):
            if o.get(key) is not None: values[key]=o[key]
        if o.get('created_by_user_id'): values['created_by_user_id']=o['created_by_user_id']
        for key in REPORT_TIMESTAMPS:
            if o.get(key): values[key]=o[key]
        timeline=o.get('timeline',[])
        for item in timeline:
            field=timestamp_by_status.get(item.get('status'))
            if field and not values[field]: values[field]=item.get('at')
            if item.get('status')=='Создана' and not values['created_by_user_id']: values['created_by_user_id']=item.get('actor_id') or o.get('created_by_user_id')
        for key,value in values.items():
            if key not in o: o[key]=value; changed=True
        if o.get('trip_status')!=o.get('status'): o['trip_status']=o.get('status'); changed=True
        started=parse_iso(o.get('trip_started_at')); completed=parse_iso(o.get('trip_completed_at')); created=parse_iso(o.get('created_at')); arrived=parse_iso(o.get('driver_arrived_at'))
        if started and completed and o.get('trip_duration_minutes') is None: o['trip_duration_minutes']=round((completed-started).total_seconds()/60,2); changed=True
        if created and arrived and o.get('waiting_duration_minutes') is None: o['waiting_duration_minutes']=round((arrived-created).total_seconds()/60,2); changed=True
        # Add stable label snapshots for completed/assigned historical records only when absent.
        for key,value in [('employee_name',employee.get('name')),('vehicle_plate',vehicle.get('plate')),('vehicle_model',vehicle.get('model')),('driver_name',driver.get('name'))]:
            if not o.get(key) and value: o[key]=value; changed=True
    return changed
def create_notification(d,recipient_id,order_id,kind,title,message,priority='normal'):
    notification={'id':str(uuid.uuid4()),'recipient_user_id':recipient_id,'order_id':order_id,'type':kind,'title':title,'message':message,'created_at':now(),'is_read':False,'priority':priority}
    d['notifications'].append(notification)
    return notification
def role_users(d,role): return [u for u in d['users'] if u['role']==role]
def notify_order_participants(d,o,kind,title,message,include_dispatchers=False):
    recipient_ids={o['employee_id']}
    if o.get('driver_id'): recipient_ids.add(o['driver_id'])
    if include_dispatchers: recipient_ids.update(u['id'] for u in role_users(d,'dispatcher'))
    for recipient_id in recipient_ids:
        create_notification(d,recipient_id,o['id'],kind,title,message)
def requested_time(o):
    return o.get('scheduled_at') or 'Сразу после назначения'
def sync_driver_vehicle(d,driver_id):
    driver=next((u for u in d['users'] if u['id']==driver_id),None)
    if not driver: return
    shift=d['shifts'].get(driver_id,{}).get('status','off')
    vehicle=next((v for v in d['vehicles'] if v.get('driver_id')==driver_id),None)
    active=next((o for o in d['orders'] if o.get('driver_id')==driver_id and o.get('status') in ACTIVE),None)
    driver['current_order_id']=active['id'] if active else None
    if active:
        driver['availability']='busy' if active['status']=='Поездка началась' else 'assigned'
        if vehicle:
            vehicle['active_order_id']=active['id']; vehicle['status']=driver['availability']
    else:
        driver['availability']=shift if shift in ('active','break') else 'offline'
        if vehicle:
            vehicle['active_order_id']=None
            if vehicle.get('status')!='maintenance': vehicle['status']='available' if shift=='active' else 'break' if shift=='break' else 'offline'
def assign_order_record(d,o,v,method='automatic',actor_id='system:auto-dispatch'):
    driver=v['driver']; stamp=now()
    position=vehicle_position(v)
    distance=None
    if o.get('pickup_latitude') is not None and position['latitude'] is not None:
        distance=VehicleAssignmentService.recommend({'users':d['users'],'vehicles':[v],'shifts':d['shifts'],'orders':[]},o)[0].get('approximate_distance_km')
    o.update(vehicle_id=v['id'],driver_id=driver['id'],driver_name=driver['name'],vehicle_plate=v.get('plate'),vehicle_model=v.get('model'),vehicle_class=v.get('class'),status=STATES[2],trip_status=STATES[2],updated_at=stamp,assignment_method=method,assignment_distance_km=distance)
    if not o.get('assigned_at'): o['assigned_at']=stamp
    v.update(status='assigned',active_order_id=o['id']); driver['current_order_id']=o['id']; driver['availability']='assigned'
    event(d,'assigned',actor_id,o['id'],{'vehicle_id':v['id'],'method':method,'distance_km':distance})
    o.setdefault('timeline',[]).append({'status':o['status'],'at':stamp,'actor':'\u0421\u0438\u0441\u0442\u0435\u043c\u0430 \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438' if method=='automatic' else '\u0414\u0438\u0441\u043f\u0435\u0442\u0447\u0435\u0440','actor_id':actor_id,'vehicle_id':v['id']})
    route=' ? '.join([o.get('pickup',''),*(x.get('address','') for x in o.get('stops',[])),o.get('destination','')])
    create_notification(d,driver['id'],o['id'],'trip_assigned','\u0412\u0430\u043c \u043d\u0430\u0437\u0447\u0435\u043d\u0430 \u043d\u043e\u0432\u0430\u044f \u043f\u043e\u0435\u0437\u0434\u043a\u0430',f'\u041c\u0430\u0440\u0448\u0440\u0443\u0442: {route}.')
    create_notification(d,o['employee_id'],o['id'],'vehicle_assigned','\u0412\u0430\u043c \u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d \u0430\u0432\u0442\u043e\u043c\u043e\u0431\u0438\u043b\u044c',f"\u041d\u0430\u0437\u043d\u0430\u0447\u0435\u043d \u0430\u0432\u0442\u043e\u043c\u043e\u0431\u0438\u043b\u044c {v.get('make','')} {v.get('model','')}, \u043d\u043e\u043c\u0435\u0440 {v.get('plate','')}")
    return o

def dispatch_queued_orders(d):
    settings=d.get('settings',{})
    if settings.get('dispatch_mode')!='automatic' or not settings.get('demo_auto_approval',True): return []
    queued=sorted((o for o in d.get('orders',[]) if o.get('status')==STATES[1] and o.get('approved',True)),key=lambda o:(o.get('created_at') or '',o.get('id') or ''))
    assigned=[]
    for order in queued:
        candidates=VehicleAssignmentService.recommend(d,order)
        if not candidates: break
        assign_order_record(d,order,candidates[0],'automatic')
        assigned.append(order['id'])
    return assigned

def read():
    with LOCK:
        if not DATA.exists() or DATA.stat().st_size==0:
            d=initial(); ensure_order_schema(d); write(d); return d
        d=json.loads(DATA.read_text(encoding='utf-8'))
        fleet_changed=ensure_fleet(d); profile_changed=ensure_profile(d); notifications_changed=ensure_notifications(d); orders_changed=ensure_order_schema(d)
        if fleet_changed or profile_changed or notifications_changed or orders_changed: write(d)
        return d
def write(d):
    DATA.parent.mkdir(parents=True,exist_ok=True)
    tmp=DATA.with_suffix('.tmp'); tmp.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8'); os.replace(tmp,DATA)
def event(d,kind,actor,order=None,detail=None):
    e={'id':str(uuid.uuid4()),'type':kind,'actor_id':actor,'order_id':order,'detail':detail,'at':now()}; d['events'].append(e); return e
def user_for(role): return next(x for x in read()['users'] if x['role']==role)
app=FastAPI(title='Qarmet Corporate Taxi API',version='1.0.0')
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_credentials=True,allow_methods=['*'],allow_headers=['*'])
@app.middleware('http')
async def serialize_mutations(request, call_next):
    # Hold one process-wide lock over each mutation's read/validate/write sequence.
    if request.method not in ('POST','PUT','PATCH','DELETE'):
        return await call_next(request)
    async with MUTATION_LOCK:
        return await call_next(request)
@app.on_event('startup')
def seed(): read()
def current_user(authorization:str|None=Header(default=None)):
    if not authorization or not authorization.lower().startswith('bearer '): raise HTTPException(401,'Требуется вход')
    d=read(); uid=d['sessions'].get(authorization[7:]); u=next((u for u in d['users'] if u['id']==uid),None)
    if not u: raise HTTPException(401,'Сессия недействительна')
    return u
def require(*roles):
    def dep(u=Depends(current_user)):
        if u['role'] not in roles: raise HTTPException(403,'Недостаточно прав')
        return u
    return dep
def order_or_404(d,oid):
    o=next((o for o in d['orders'] if o['id']==oid),None)
    if not o: raise HTTPException(404,'Заказ не найден')
    return o
class TripStop(BaseModel):
    address:str=Field(min_length=1,max_length=240); latitude:float|None=Field(default=None,ge=-90,le=90); longitude:float|None=Field(default=None,ge=-180,le=180)
    @model_validator(mode='after')
    def coordinate_pair(self):
        if (self.latitude is None)!=(self.longitude is None): raise ValueError('\u041a\u043e\u043e\u0440\u0434\u0438\u043d\u0430\u0442\u044b \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0438 \u043d\u0443\u0436\u043d\u043e \u0443\u043a\u0430\u0437\u0430\u0442\u044c \u0432\u043c\u0435\u0441\u0442\u0435')
        if not self.address.strip(): raise ValueError('\u0423\u043a\u0430\u0436\u0438\u0442\u0435 \u0430\u0434\u0440\u0435\u0441 \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0438')
        return self
class Trip(BaseModel):
    pickup:str; destination:str; stop:str|None=None; stops:list[TripStop]=Field(default_factory=list,max_length=10); scheduled_at:str|None=None; purpose:str=''; vehicle_class:str='\u041b\u044e\u0431\u043e\u0439'; passengers:int=Field(1,ge=1,le=8); comment:str=''; employee_id:str|None=None
    pickup_latitude:float|None=Field(default=None,ge=-90,le=90); pickup_longitude:float|None=Field(default=None,ge=-180,le=180)
    destination_latitude:float|None=Field(default=None,ge=-90,le=90); destination_longitude:float|None=Field(default=None,ge=-180,le=180); cost_center:str|None=None
    @model_validator(mode='after')
    def validate_coordinate_pairs(self):
        if (self.pickup_latitude is None)!=(self.pickup_longitude is None): raise ValueError('\u041a\u043e\u043e\u0440\u0434\u0438\u043d\u0430\u0442\u044b \u0442\u043e\u0447\u043a\u0438 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f \u043d\u0443\u0436\u043d\u043e \u0443\u043a\u0430\u0437\u0430\u0442\u044c \u0432\u043c\u0435\u0441\u0442\u0435')
        if (self.destination_latitude is None)!=(self.destination_longitude is None): raise ValueError('\u041a\u043e\u043e\u0440\u0434\u0438\u043d\u0430\u0442\u044b \u043f\u0443\u043d\u043a\u0442\u0430 \u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u044f \u043d\u0443\u0436\u043d\u043e \u0443\u043a\u0430\u0437\u0430\u0442\u044c \u0432\u043c\u0435\u0441\u0442\u0435')
        route_addresses=[self.pickup.strip().casefold(),*(point.address.strip().casefold() for point in self.stops),self.destination.strip().casefold()]
        if any(a==b for a,b in zip(route_addresses,route_addresses[1:])): raise ValueError('\u0421\u043e\u0441\u0435\u0434\u043d\u0438\u0435 \u0442\u043e\u0447\u043a\u0438 \u043c\u0430\u0440\u0448\u0440\u0443\u0442\u0430 \u0434\u043e\u043b\u0436\u043d\u044b \u0440\u0430\u0437\u043b\u0438\u0447\u0430\u0442\u044c\u0441\u044f')
        return self
class Reason(BaseModel): reason:str=Field(min_length=2)
class Location(BaseModel): latitude:float=Field(ge=-90,le=90); longitude:float=Field(ge=-180,le=180)
class Incident(BaseModel): kind:Literal['breakdown','sos']; message:str=''
class Assignment(BaseModel): vehicle_id:str|None=None
@app.post('/api/auth/demo')
def login(body:dict):
    role=body.get('role')
    if role not in ROLES: raise HTTPException(422,'Неизвестная роль')
    d=read(); user_id=body.get('user_id'); user=next((u for u in d['users'] if u['role']==role and (not user_id or u['id']==user_id)),None)
    if not user: raise HTTPException(422,'Пользователь для входа не найден')
    token=uuid.uuid4().hex; d['sessions'][token]=user['id']; event(d,'login',user['id']); write(d); return {'token':token,'user':user}
@app.get('/api/auth/demo-drivers')
def demo_drivers(): return [{'id':u['id'],'name':u['name'],'employee_number':u['employee_number']} for u in read()['users'] if u['role']=='driver']
@app.get('/api/me')
def me(u=Depends(current_user)): return u
@app.post('/api/auth/logout')
def logout(u=Depends(current_user),authorization:str=Header()):
    d=read(); d['sessions'].pop(authorization[7:],None); write(d); return {'ok':True}
def vehicle_view(d,vehicle):
    driver=next((x for x in d.get('users',[]) if x['id']==vehicle.get('driver_id')),None)
    position=vehicle_position(vehicle)
    return {**vehicle,'current_latitude':position['latitude'],'current_longitude':position['longitude'],'location_updated_at':position['updated_at'],'location_source':position['source'],'location_source_label':'\u0414\u0435\u043c\u043e-\u0433\u0435\u043e\u043b\u043e\u043a\u0430\u0446\u0438\u044f' if position['source']=='demo' else 'GPS' if position['source']=='gps' else '\u041d\u0435\u0442 \u043a\u043e\u043e\u0440\u0434\u0438\u043d\u0430\u0442','location_fresh':position['fresh'],'driver_name':driver.get('name') if driver else None,'driver_employee_number':driver.get('employee_number') if driver else None,'shift_status':d.get('shifts',{}).get(vehicle.get('driver_id'),{}).get('status','off')}

@app.get('/api/settings/dispatch')
def get_dispatch_settings(u=Depends(require('dispatcher'))):
    d=read(); settings=d.get('settings',{})
    return {'mode':settings.get('dispatch_mode','manual'),'demo_auto_approval':settings.get('demo_auto_approval',True)}

class DispatchSettingsUpdate(BaseModel): mode:Literal['automatic','manual']
@app.patch('/api/settings/dispatch')
def update_dispatch_settings(body:DispatchSettingsUpdate,u=Depends(require('dispatcher'))):
    d=read(); d.setdefault('settings',{})['dispatch_mode']=body.mode
    assigned=dispatch_queued_orders(d) if body.mode=='automatic' else []
    write(d)
    return {'mode':body.mode,'demo_auto_approval':d['settings'].get('demo_auto_approval',True),'assigned_order_ids':assigned}

@app.get('/api/bootstrap')
def bootstrap(u=Depends(current_user)):
    d=read(); orders=d['orders'] if u['role']=='dispatcher' else [o for o in d['orders'] if o['employee_id']==u['id'] or (u['role']=='driver' and (o.get('driver_id')==u['id'] or o['status']=='Назначена машина'))]
    if u['role']=='driver':
        orders=[{**o, **({'employee_name':'Сотрудник','comment':''} if o.get('driver_id')==u['id'] and o['status']=='Назначена машина' else {})} for o in orders]
    user_notifications=sorted((n for n in d['notifications'] if n['recipient_user_id']==u['id']),key=lambda n:n['created_at'],reverse=True)
    visible_vehicles=[vehicle_view(d,v) for v in d['vehicles']] if u['role']=='dispatcher' else [vehicle_view(d,v) for v in d['vehicles'] if u['role']=='driver' and v.get('driver_id')==u['id']]
    if u['role']=='employee': visible_vehicles=[{k:v for k,v in vehicle.items() if k not in ('current_latitude','current_longitude','location_updated_at','location_source','location_source_label','current_location','demo_latitude','demo_longitude')} for vehicle in (vehicle_view(d,v) for v in d['vehicles'])]
    visible_users=d['users'] if u['role']=='dispatcher' else [u]
    can_assign=any(o['status']=='Ожидает обработки' and VehicleAssignmentService.eligible_vehicles(d,o) for o in orders) if u['role']=='dispatcher' else False
    return {'user':u,'orders':orders,'vehicles':visible_vehicles,'users':visible_users,'shift':d['shifts'].get(u['id']) if u['role']=='driver' else d['shifts'].get('driver-1') if u['role']=='dispatcher' else None,'can_assign':can_assign,'notifications':user_notifications,'unread_count':sum(not n['is_read'] for n in user_notifications),'locations':d['locations'],'dispatch_settings':d.get('settings',{}),'events':d['events'][-100:],'incidents':d['incidents'] if u['role']=='dispatcher' else []}
@app.get('/api/locations')
def locations(u=Depends(current_user)): return read()['locations']
def nominatim_request(path,params):
    global GEOCODER_LAST_REQUEST
    key=path+'?'+urllib.parse.urlencode(params)
    with GEOCODER_LOCK:
        if key in GEOCODER_CACHE: return GEOCODER_CACHE[key]
        wait=1.0-(time.monotonic()-GEOCODER_LAST_REQUEST)
        if wait>0: time.sleep(wait)
        GEOCODER_LAST_REQUEST=time.monotonic()
        url=f"{GEOCODER_BASE}{path}?{urllib.parse.urlencode(params)}"
        request=urllib.request.Request(url,headers={'User-Agent':'QarmetCorporateTaxi/1.0 (internal fleet application)','Accept-Language':'ru,en','Accept':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=8) as response: result=json.loads(response.read().decode('utf-8'))
        except (urllib.error.URLError,TimeoutError,json.JSONDecodeError) as exc: raise HTTPException(502,'Сервис поиска адресов временно недоступен; выберите точку вручную на карте') from exc
        GEOCODER_CACHE[key]=result
        return result
@app.get('/api/geocode/search')
def geocode_search(q:str=Query(min_length=3,max_length=180),u=Depends(current_user)):
    return {'results':nominatim_request('/search',{'format':'jsonv2','addressdetails':1,'limit':5,'countrycodes':'kz','q':q})}
@app.get('/api/geocode/reverse')
def geocode_reverse(latitude:float=Query(ge=-90,le=90),longitude:float=Query(ge=-180,le=180),u=Depends(current_user)):
    result=nominatim_request('/reverse',{'format':'jsonv2','lat':latitude,'lon':longitude,'zoom':18,'addressdetails':1})
    return {'result':result}
@app.get('/api/employees')
def employees(u=Depends(require('dispatcher'))): return [x for x in read()['users'] if x['role']=='employee']
@app.get('/api/drivers')
def drivers(u=Depends(require('dispatcher'))):
    d=read(); results=[]
    for driver in d['users']:
        if driver.get('role')!='driver': continue
        vehicle=next((v for v in d['vehicles'] if v.get('driver_id')==driver['id']),None)
        results.append({'user':driver,'shift':d['shifts'].get(driver['id']), 'vehicle':vehicle,'availability':driver.get('availability','offline'),'current_order_id':driver.get('current_order_id')})
    return results
@app.get('/api/vehicles')
def vehicles(u=Depends(current_user)):
    d=read(); result=[]
    for vehicle in d['vehicles']:
        item=vehicle_view(d,vehicle)
        if u['role']=='employee': item={k:v for k,v in item.items() if k not in ('current_latitude','current_longitude','location_updated_at','location_source','location_source_label','driver_employee_number','demo_latitude','demo_longitude')}
        elif u['role']=='driver' and vehicle.get('driver_id')!=u['id']: continue
        result.append(item)
    return result
@app.get('/api/vehicles/recommendations')
def vehicle_recommendations(order_id:str|None=None,pickup_latitude:float|None=Query(default=None,ge=-90,le=90),pickup_longitude:float|None=Query(default=None,ge=-180,le=180),u=Depends(require('dispatcher'))):
    d=read(); order=order_or_404(d,order_id) if order_id else {'passengers':1,'vehicle_class':'Любой'}
    return VehicleAssignmentService.recommend(d,order,pickup_latitude,pickup_longitude)
class DriverVehicleAssignment(BaseModel): driver_id:str
@app.patch('/api/vehicles/{vehicle_id}/driver')
def assign_driver_vehicle(vehicle_id:str,body:DriverVehicleAssignment,u=Depends(require('dispatcher'))):
    d=read(); vehicle=next((v for v in d['vehicles'] if v['id']==vehicle_id),None)
    if not vehicle: raise HTTPException(404,'Автомобиль не найден')
    driver=next((x for x in d['users'] if x['id']==body.driver_id and x['role']=='driver'),None)
    if not driver: raise HTTPException(404,'Водитель не найден')
    if any(o.get('vehicle_id')==vehicle_id and o.get('status') in ACTIVE for o in d['orders']): raise HTTPException(409,'Нельзя менять водителя у автомобиля с активной поездкой')
    if d['shifts'].get(vehicle.get('driver_id'),{}).get('status') in ('active','break') or d['shifts'].get(driver['id'],{}).get('status') in ('active','break'): raise HTTPException(409,'Для переназначения автомобиля смены обоих водителей должны быть завершены')
    other=next((v for v in d['vehicles'] if v.get('driver_id')==driver['id'] and v['id']!=vehicle_id),None)
    if other: raise HTTPException(409,'Водитель уже закреплён за другим автомобилем')
    previous=next((x for x in d['users'] if x['id']==vehicle.get('driver_id')),None)
    if previous: previous['vehicle_id']=None; previous['assigned_vehicle_id']=None; previous['availability']='offline'
    vehicle['driver_id']=driver['id']
    if vehicle['status']!='maintenance': vehicle['status']='offline'
    driver['vehicle_id']=vehicle_id; driver['assigned_vehicle_id']=vehicle_id; driver['availability']='offline'; write(d)
    return {'vehicle':vehicle,'driver':driver}
@app.get('/api/orders/{oid}/eligible-vehicles')
def eligible_order_vehicles(oid:str,u=Depends(require('dispatcher'))):
    d=read(); o=order_or_404(d,oid)
    if o['status'] not in ('Ожидает обработки','Назначена машина'): return []
    return VehicleAssignmentService.recommend(d,o)
@app.get('/api/notifications')
def list_notifications(u=Depends(current_user)):
    d=read()
    return sorted((n for n in d['notifications'] if n['recipient_user_id']==u['id']),key=lambda n:n['created_at'],reverse=True)
@app.get('/api/notifications/unread-count')
def unread_notification_count(u=Depends(current_user)):
    d=read(); return {'count':sum(n['recipient_user_id']==u['id'] and not n['is_read'] for n in d['notifications'])}
@app.patch('/api/notifications/{notification_id}/read')
def mark_notification_read(notification_id:str,u=Depends(current_user)):
    d=read(); n=next((n for n in d['notifications'] if n['id']==notification_id and n['recipient_user_id']==u['id']),None)
    if not n: raise HTTPException(404,'Уведомление не найдено')
    n['is_read']=True; write(d); return n
@app.post('/api/notifications/read-all')
def mark_all_notifications_read(u=Depends(current_user)):
    d=read(); changed=0
    for n in d['notifications']:
        if n['recipient_user_id']==u['id'] and not n['is_read']: n['is_read']=True; changed+=1
    if changed: write(d)
    return {'updated':changed}
@app.get('/api/dashboard')
def dashboard(u=Depends(require('dispatcher'))):
    d=read(); orders=d['orders']; statuses=[o['status'] for o in orders]
    return {'vehicles':len(d['vehicles']),'free':sum(v['status']=='available' for v in d['vehicles']),'busy':sum(v['status']=='busy' for v in d['vehicles']),'assigned':sum(v['status']=='assigned' for v in d['vehicles']),'active':sum(s in ACTIVE for s in statuses),'queued':sum(s=='Ожидает обработки' for s in statuses),'completed_today':sum(o['status']=='Завершена' and o.get('trip_completed_at','')[:10]==now()[:10] for o in orders)}
@app.post('/api/orders',status_code=201)
def create_order(body:Trip,u=Depends(current_user)):
    if u['role'] not in ('employee','dispatcher'): raise HTTPException(403,'Заказ доступен сотруднику или диспетчеру')
    if not body.pickup.strip() or not body.destination.strip(): raise HTTPException(422,'Укажите точки маршрута')
    if body.pickup.strip()==body.destination.strip(): raise HTTPException(422,'Точки маршрута должны различаться')
    d=read()
    if body.pickup_latitude is None and body.pickup not in d['locations']: raise HTTPException(422,'Выберите точку отправления из подсказок или укажите её на карте')
    if body.destination_latitude is None and body.destination not in d['locations']: raise HTTPException(422,'Выберите пункт назначения из подсказок или укажите его на карте')
    for point in body.stops:
        if point.latitude is None and point.address not in d['locations']: raise HTTPException(422,'\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0443 \u0438\u0437 \u043f\u043e\u0434\u0441\u043a\u0430\u0437\u043e\u043a \u0438\u043b\u0438 \u0443\u043a\u0430\u0436\u0438\u0442\u0435 \u0435\u0451 \u043d\u0430 \u043a\u0430\u0440\u0442\u0435')
    if body.scheduled_at:
        try:
            dt=datetime.fromisoformat(body.scheduled_at.replace('Z','+00:00'))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
            if dt < datetime.now(timezone.utc): raise HTTPException(422,'Время поездки не может быть в прошлом')
        except ValueError: raise HTTPException(422,'Некорректная дата поездки')
    emp=body.employee_id if u['role']=='dispatcher' and body.employee_id else u['id']; emp_user=next((x for x in d['users'] if x['id']==emp and x['role']=='employee'),None)
    if not emp_user: raise HTTPException(422,'Сотрудник не найден')
    stop_records=[point.model_dump() for point in body.stops]
    if not stop_records and body.stop: stop_records=[{'address':body.stop,'latitude':None,'longitude':None}]
    route_points=[{'type':'pickup','sequence':0,'address':body.pickup,'latitude':body.pickup_latitude,'longitude':body.pickup_longitude},*({'type':'stop','sequence':index,'address':point['address'],'latitude':point.get('latitude'),'longitude':point.get('longitude')} for index,point in enumerate(stop_records,1)),{'type':'destination','sequence':len(stop_records)+1,'address':body.destination,'latitude':body.destination_latitude,'longitude':body.destination_longitude}]
    created_at=now()
    o={'id':'QT-'+uuid.uuid4().hex[:7].upper(),'employee_id':emp,'employee_name':emp_user['name'],'employee_department':emp_user.get('department'),'driver_id':None,'driver_name':None,'vehicle_id':None,'vehicle_plate':None,'vehicle_model':None,'vehicle_class':body.vehicle_class,'pickup':body.pickup,'pickup_address':body.pickup,'pickup_latitude':body.pickup_latitude,'pickup_longitude':body.pickup_longitude,'destination':body.destination,'destination_address':body.destination,'destination_latitude':body.destination_latitude,'destination_longitude':body.destination_longitude,'stop':stop_records[0]['address'] if stop_records else body.stop,'stops':stop_records,'route_points':route_points,'scheduled_at':body.scheduled_at,'assigned_at':None,'accepted_at':None,'driver_departed_at':None,'driver_arrived_at':None,'trip_started_at':None,'trip_completed_at':None,'cancelled_at':None,'cancellation_reason':None,'actual_distance_km':None,'estimated_distance_km':None,'trip_duration_minutes':None,'waiting_duration_minutes':None,'cost_center':body.cost_center,'created_by_user_id':u['id'],'assignment_method':None,'assignment_distance_km':None,'approved':True,'purpose':body.purpose,'trip_purpose':body.purpose,'passengers':body.passengers,'passenger_count':body.passengers,'comment':body.comment,'status':'Ожидает обработки','trip_status':'Ожидает обработки','created_at':created_at,'updated_at':created_at,'timeline':[]}
    d['orders'].insert(0,o); e=event(d,'order_created',u['id'],o['id']); o['timeline'].append({'status':o['status'],'at':e['at'],'actor':u['name']})
    if u['role']=='employee':
        message=f"Маршрут: {o['pickup']} — {o['destination']}. Время: {requested_time(o)}."
        for dispatcher in role_users(d,'dispatcher'): create_notification(d,dispatcher['id'],o['id'],'order_created','Новая заявка на поездку',message)
    if d.get('settings',{}).get('demo_auto_approval',True): dispatch_queued_orders(d)
    write(d); return o
@app.get('/api/orders')
def list_orders(u=Depends(current_user),status:str|None=None,q:str|None=None):
    d=read(); result=d['orders']
    if u['role']=='employee': result=[o for o in result if o['employee_id']==u['id']]
    elif u['role']=='driver': result=[o for o in result if o.get('driver_id')==u['id'] or o['status']=='Назначена машина']
    if status: result=[o for o in result if o['status']==status]
    if q: result=[o for o in result if q.lower() in json.dumps(o,ensure_ascii=False).lower()]
    if u['role']=='driver': result=[{**o,**({'employee_name':'Сотрудник','comment':''} if o.get('driver_id')==u['id'] and o['status']=='Назначена машина' else {})} for o in result]
    return result
@app.get('/api/orders/{oid}')
def get_order(oid:str,u=Depends(current_user)):
    d=read(); o=order_or_404(d,oid)
    if u['role']=='employee' and o['employee_id']!=u['id']: raise HTTPException(403,'Нет доступа')
    if u['role']=='driver' and o.get('driver_id') not in (None,u['id']): raise HTTPException(403,'Нет доступа')
    return o
@app.post('/api/orders/{oid}/assign')
def assign(oid:str,body:Assignment|None=Body(default=None),u=Depends(require('dispatcher'))):
    d=read(); o=order_or_404(d,oid)
    if o['status'] not in ('Ожидает обработки','Назначена машина'): raise HTTPException(409,'Заказ нельзя назначить в текущем статусе')
    candidates=VehicleAssignmentService.recommend(d,o)
    selected_id=body.vehicle_id if body else None
    v=next((candidate for candidate in candidates if candidate['id']==selected_id),None) if selected_id else (candidates[0] if candidates else None)
    if selected_id and not v: raise HTTPException(409,'Выбранный автомобиль или водитель сейчас недоступен')
    if not v: raise HTTPException(409,'Нет автомобиля со свободным водителем на активной смене; заказ остаётся в очереди')
    previous_driver=o.get('driver_id'); previous_vehicle=next((x for x in d['vehicles'] if x['id']==o.get('vehicle_id')),None)
    if previous_vehicle and previous_vehicle['id']!=v['id']:
        old_driver=previous_vehicle.get('driver_id'); previous_vehicle['active_order_id']=None; o['driver_id']=v['driver_id']; o['vehicle_id']=v['id']
        if old_driver: sync_driver_vehicle(d,old_driver); create_notification(d,old_driver,oid,'assignment_changed','Назначение изменено','Диспетчер переназначил автомобиль для поездки.')
    driver=v['driver']; stamp=now()
    o.update(vehicle_id=v['id'],driver_id=driver['id'],driver_name=driver['name'],vehicle_plate=v['plate'],vehicle_model=v['model'],vehicle_class=v['class'],status='Назначена машина',trip_status='Назначена машина',updated_at=stamp,assignment_method='manual',assignment_distance_km=v.get('approximate_distance_km'))
    if not o.get('assigned_at'): o['assigned_at']=stamp
    v.update(status='assigned',active_order_id=oid); driver['current_order_id']=oid; driver['availability']='assigned'
    event(d,'assigned',u['id'],oid,v['id']); o['timeline'].append({'status':o['status'],'at':stamp,'actor':u['name'],'vehicle_id':v['id']})
    trip_details=f"Маршрут: {o['pickup']} — {o['destination']}. Время: {requested_time(o)}."
    create_notification(d,driver['id'],oid,'trip_assigned','Вам назначена новая поездка',trip_details)
    create_notification(d,o['employee_id'],oid,'vehicle_assigned','Ваша машина назначена',f"Назначен автомобиль {v['make']} {v['model']}, номер {v['plate']}.")
    write(d); return o
@app.post('/api/orders/{oid}/transition')
def transition(oid:str,body:dict,u=Depends(current_user)):
    d=read(); o=order_or_404(d,oid); action=body.get('action'); role=u['role']; shift=d['shifts'].get(u['id'],{})
    if role!='driver' or o.get('driver_id')!=u['id']: raise HTTPException(403,'Переход доступен назначенному водителю')
    if action=='reject':
        if o['status']!='Назначена машина': raise HTTPException(409,'Отклонить можно только новое назначение')
        reason=body.get('reason','').strip()
        if len(reason)<2: raise HTTPException(422,'Укажите причину отказа')
        old_driver=o.get('driver_id'); old_vehicle=next((v for v in d['vehicles'] if v['id']==o.get('vehicle_id')),None)
        o.update(status='Ожидает обработки',trip_status='Ожидает обработки',vehicle_id=None,driver_id=None,driver_name=None,vehicle_plate=None,vehicle_model=None,vehicle_class=o.get('vehicle_class'),updated_at=now(),rejection_reason=reason)
        if old_vehicle: old_vehicle['active_order_id']=None
        if old_driver: sync_driver_vehicle(d,old_driver)
        event(d,'rejected',u['id'],oid,reason); o['timeline'].append({'status':o['status'],'at':now(),'actor':u['name'],'reason':reason})
        notify_order_participants(d,o,'assignment_rejected','Назначение отклонено',f"Водитель отклонил назначение. Причина: {reason}",include_dispatchers=True); write(d); return o
    allowed={'accept':('Назначена машина','Водитель принял заказ'),'depart':('Водитель принял заказ','Водитель в пути'),'arrive':('Водитель в пути','Водитель на месте'),'start':('Водитель на месте','Поездка началась'),'complete':('Поездка началась','Завершена')}
    if action not in allowed or o['status']!=allowed[action][0]: raise HTTPException(409,'Недопустимый переход статуса')
    if shift['status']!='active': raise HTTPException(409,'Смена не активна')
    o['status']=allowed[action][1]; o['trip_status']=o['status']; o['updated_at']=now()
    timestamp_field={'accept':'accepted_at','depart':'driver_departed_at','arrive':'driver_arrived_at','start':'trip_started_at','complete':'trip_completed_at'}[action]
    if not o.get(timestamp_field): o[timestamp_field]=o['updated_at']
    event(d,action,u['id'],oid); o['timeline'].append({'status':o['status'],'at':o['updated_at'],'actor':u['name'],'actor_id':u['id']})
    employee_title={'accept':'Водитель принял заказ','depart':'Водитель направляется к вам','arrive':'Водитель прибыл','start':'Поездка началась','complete':'Поездка завершена'}[action]
    employee_message=f"{employee_title}."
    if action=='arrive': employee_message+=f" Место встречи: {o['pickup']}."
    create_notification(d,o['employee_id'],oid,action,employee_title,employee_message)
    if action in ('start','complete'):
        dispatcher_title='Поездка началась' if action=='start' else 'Заказ завершён'
        dispatcher_message=f"Заказ {o['id']}: {o['pickup']} — {o['destination']}."
        for dispatcher in role_users(d,'dispatcher'): create_notification(d,dispatcher['id'],oid,action,dispatcher_title,dispatcher_message)
    if action=='complete':
        started=parse_iso(o.get('trip_started_at')); completed=parse_iso(o.get('trip_completed_at')); created=parse_iso(o.get('created_at')); arrived=parse_iso(o.get('driver_arrived_at'))
        if started and completed: o['trip_duration_minutes']=round((completed-started).total_seconds()/60,2)
        if created and arrived: o['waiting_duration_minutes']=round((arrived-created).total_seconds()/60,2)
        sync_driver_vehicle(d,u['id'])
        dispatch_queued_orders(d)
    else: sync_driver_vehicle(d,u['id'])
    write(d); return o
@app.post('/api/orders/{oid}/cancel')
def cancel(oid:str,body:Reason,u=Depends(current_user)):
    d=read(); o=order_or_404(d,oid)
    if u['role']=='employee' and o['employee_id']!=u['id']: raise HTTPException(403,'Нет доступа')
    if u['role'] not in ('employee','dispatcher'): raise HTTPException(403,'Отмена недоступна')
    if o['status'] in ('Поездка началась','Завершена','Отменена'): raise HTTPException(409,'Поездку нельзя отменить')
    old=o['status']; old_driver=o.get('driver_id'); cancelled_at=now(); o.update(status='Отменена',trip_status='Отменена',updated_at=cancelled_at,cancelled_at=cancelled_at,cancel_reason=body.reason,cancellation_reason=body.reason); event(d,'cancelled',u['id'],o['id'],body.reason); o['timeline'].append({'status':'Отменена','at':cancelled_at,'actor':u['name'],'reason':body.reason,'actor_id':u['id']})
    if old in ACTIVE and old_driver: sync_driver_vehicle(d,old_driver)
    dispatch_queued_orders(d)
    notify_order_participants(d,o,'order_cancelled','Поездка отменена',f"Заказ отменён. Причина: {body.reason}",include_dispatchers=True)
    write(d); return o
@app.post('/api/driver/shift/{action}')
def shift_action(action:str,u=Depends(require('driver'))):
    if action not in ('start','break','resume','end'): raise HTTPException(404,'Неизвестное действие')
    d=read(); s=d['shifts'][u['id']]; expected={'start':'off','break':'active','resume':'break','end':'active'}; target={'start':'active','break':'break','resume':'active','end':'off'}
    if s['status']!=expected[action]: raise HTTPException(409,'Недопустимое действие со сменой')
    if action=='break' and any(o.get('driver_id')==u['id'] and o['status'] in ACTIVE for o in d['orders']): raise HTTPException(409,'Нельзя начать перерыв во время назначенной поездки')
    if action=='end' and any(o.get('driver_id')==u['id'] and o['status'] in ACTIVE for o in d['orders']): raise HTTPException(409,'Завершите активную поездку перед окончанием смены')
    s.update(status=target[action],updated_at=now()); event(d,'shift_'+action,u['id']); sync_driver_vehicle(d,u['id'])
    if action in ('start','resume'): dispatch_queued_orders(d)
    write(d); return s
@app.post('/api/driver/location')
def location(body:Location,u=Depends(require('driver'))):
    d=read(); s=d['shifts'][u['id']]
    if s['status']!='active': raise HTTPException(409,'Геопозиция передается только во время смены')
    vehicle=next((v for v in d['vehicles'] if v.get('driver_id')==u['id']),None)
    if not vehicle: raise HTTPException(409,'У водителя нет закреплённого автомобиля')
    vehicle.update(current_latitude=body.latitude,current_longitude=body.longitude,location_updated_at=now(),location_source='browser_gps')
    vehicle['current_location']={'latitude':body.latitude,'longitude':body.longitude,'updated_at':vehicle['location_updated_at'],'source':'browser_gps'}; event(d,'location_updated',u['id'],detail={'vehicle_id':vehicle['id'],'latitude':body.latitude,'longitude':body.longitude,'updated_at':vehicle['location_updated_at']}); write(d); return {'ok':True}
@app.post('/api/incidents',status_code=201)
def incident(body:Incident,u=Depends(require('driver'))):
    d=read(); item={'id':str(uuid.uuid4()),'driver_id':u['id'],'kind':body.kind,'message':body.message,'at':now(),'status':'new'}; d['incidents'].insert(0,item); event(d,'incident',u['id'],detail=item)
    title='Экстренный сигнал SOS' if body.kind=='sos' else 'Сообщение о поломке'
    message=body.message.strip() or title
    for dispatcher in role_users(d,'dispatcher'): create_notification(d,dispatcher['id'],None,body.kind,title,message,priority='high')
    write(d); return item
def report_data(date_from,date_to,driver_id,vehicle_id,employee_id,status,department):
    d=read(); orders=filtered_orders(d,date_from,date_to,driver_id,vehicle_id,employee_id,status,department)
    datasets=normalized_datasets(d,orders)
    return d,orders,datasets
@app.get('/api/reports/summary')
def report_summary(date_from:date|None=None,date_to:date|None=None,driver_id:str|None=None,vehicle_id:str|None=None,employee_id:str|None=None,status:str|None=None,department:str|None=None,u=Depends(require('dispatcher'))):
    d,orders,datasets=report_data(date_from,date_to,driver_id,vehicle_id,employee_id,status,department)
    def group(key,label_key=None):
        counts={}
        for o in orders:
            value=o.get(key) or 'Не указано'; label=o.get(label_key) if label_key else value
            bucket=counts.setdefault(value,{'id':value,'name':label or 'Не указано','count':0}); bucket['count']+=1
        return sorted(counts.values(),key=lambda x:(-x['count'],str(x['name'])))
    by_date={}
    for o in orders:
        dt=to_local_datetime(o.get('created_at')); label=dt.date().isoformat() if dt else 'Не указано'; by_date[label]=by_date.get(label,0)+1
    waits=[o['waiting_duration_minutes'] for o in orders if isinstance(o.get('waiting_duration_minutes'),(int,float))]
    utilization=[]
    if date_from and date_to and date_to>=date_from:
        period_minutes=max(1,((datetime.combine(date_to+timedelta(days=1),time.min,tzinfo=REPORT_TZ)-datetime.combine(date_from,time.min,tzinfo=REPORT_TZ)).total_seconds()/60))
        for v in d['vehicles']:
            minutes=sum(o.get('trip_duration_minutes') or 0 for o in orders if o.get('vehicle_id')==v['id'] and o.get('trip_duration_minutes') is not None)
            utilization.append({'vehicle_id':v['id'],'vehicle':f"{v.get('make','')} {v.get('model','')}".strip(),'utilization_percent':round(minutes/period_minutes*100,2) if minutes else 0})
    return {'total_trips':len(orders),'completed_trips':sum(o.get('status')=='Завершена' for o in orders),'cancelled_trips':sum(o.get('status')=='Отменена' for o in orders),'average_waiting_minutes':round(sum(waits)/len(waits),2) if waits else None,'trips_by_driver':group('driver_id','driver_name'),'trips_by_vehicle':group('vehicle_id','vehicle_plate'),'trips_by_employee':group('employee_id','employee_name'),'trips_by_department':group('employee_department'),'trips_by_date':[{'date':k,'count':v} for k,v in sorted(by_date.items())],'vehicle_utilization':utilization if date_from and date_to else None,'trips':datasets['Поездки'],'filters':{'drivers':[{'id':x['id'],'name':x.get('name')} for x in d['users'] if x['role']=='driver'],'vehicles':[{'id':x['id'],'name':f"{x.get('plate')} · {x.get('make')} {x.get('model')}"} for x in d['vehicles']],'employees':[{'id':x['id'],'name':x.get('name')} for x in d['users'] if x['role']=='employee'],'departments':sorted({x.get('department') for x in d['users'] if x['role']=='employee' and x.get('department')}),'statuses':['Ожидает назначения','Назначена','В пути','Завершена','Отменена']}}
def report_query_args(date_from,date_to,driver_id,vehicle_id,employee_id,status,department):
    return {'date_from':date_from,'date_to':date_to,'driver_id':driver_id,'vehicle_id':vehicle_id,'employee_id':employee_id,'status':status,'department':department}
@app.get('/api/reports/export/xlsx')
def export_reports_xlsx(date_from:date|None=None,date_to:date|None=None,driver_id:str|None=None,vehicle_id:str|None=None,employee_id:str|None=None,status:str|None=None,department:str|None=None,u=Depends(require('dispatcher'))):
    if date_from and date_to and date_from>date_to: raise HTTPException(422,'Начало периода позже окончания')
    d,orders,datasets=report_data(**report_query_args(date_from,date_to,driver_id,vehicle_id,employee_id,status,department)); payload=xlsx_bytes(datasets)
    return StreamingResponse(io.BytesIO(payload),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+urllib.parse.quote('Отчёт_по_поездкам.xlsx')})
@app.get('/api/reports/export/csv')
def export_reports_csv(date_from:date|None=None,date_to:date|None=None,driver_id:str|None=None,vehicle_id:str|None=None,employee_id:str|None=None,status:str|None=None,department:str|None=None,u=Depends(require('dispatcher'))):
    if date_from and date_to and date_from>date_to: raise HTTPException(422,'Начало периода позже окончания')
    d,orders,datasets=report_data(**report_query_args(date_from,date_to,driver_id,vehicle_id,employee_id,status,department)); payload=csv_zip_bytes(datasets)
    return StreamingResponse(io.BytesIO(payload),media_type='application/zip',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+urllib.parse.quote('Данные_для_отчётности.zip')})
@app.get('/api/audit')
def audit(u=Depends(require('dispatcher'))): return read()['events']

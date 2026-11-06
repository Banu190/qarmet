"""Structured, Russian-language reports over records persisted by the API."""
from __future__ import annotations
import csv, io, zipfile
from datetime import datetime, date
from zoneinfo import ZoneInfo
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

REPORT_TZ=ZoneInfo('Asia/Almaty')
TIME_COLUMNS={
    'created_at','scheduled_at','assigned_at','accepted_at','driver_departed_at',
    'driver_arrived_at','trip_started_at','trip_completed_at','cancelled_at','occurred_at',
}
TRIP_COLUMNS=('order_id','employee_id','employee_name','employee_department','driver_id','driver_name','vehicle_id','vehicle_plate','vehicle_model','vehicle_class','pickup_address','pickup_latitude','pickup_longitude','stop','destination_address','destination_latitude','destination_longitude','route','passenger_count','trip_purpose','created_at','scheduled_at','assigned_at','accepted_at','driver_departed_at','driver_arrived_at','trip_started_at','trip_completed_at','cancelled_at','cancellation_reason','trip_status','actual_distance_km','estimated_distance_km','trip_duration_minutes','waiting_duration_minutes','cost_center','created_by_user_id','assignment_method','assignment_distance_km')
DRIVER_COLUMNS=('driver_id','full_name','employee_number','department','assigned_vehicle_id','shift_status','availability','current_order_id','phone','email')
VEHICLE_COLUMNS=('vehicle_id','license_plate','make','model','vehicle_class','passenger_capacity','assigned_driver_id','status','current_latitude','current_longitude','location_timestamp','garage','active_order_id')
EMPLOYEE_COLUMNS=('employee_id','full_name','employee_number','department')
STOP_COLUMNS=('stop_id','order_id','stop_sequence','intermediate_stop','latitude','longitude')
EVENT_COLUMNS=('trip_event_id','order_id','event_type','trip_status','occurred_at','actor_user_id','actor_name','reason')

DATASET_COLUMNS={'Поездки':TRIP_COLUMNS,'Водители':DRIVER_COLUMNS,'Автомобили':VEHICLE_COLUMNS,'Сотрудники':EMPLOYEE_COLUMNS,'Остановки':STOP_COLUMNS,'История событий':EVENT_COLUMNS}
CSV_FILENAMES={'Поездки':'Поездки.csv','Водители':'Водители.csv','Автомобили':'Автомобили.csv','Сотрудники':'Сотрудники.csv','Остановки':'Остановки.csv','История событий':'История_событий.csv'}
HEADERS={
    'order_id':'Номер заявки','employee_id':'Код сотрудника','employee_name':'Сотрудник','employee_department':'Подразделение',
    'driver_id':'Код водителя','driver_name':'Водитель','vehicle_id':'Код автомобиля','vehicle_plate':'Госномер','vehicle_model':'Автомобиль','vehicle_class':'Класс автомобиля',
    'pickup_address':'Пункт отправления','pickup_latitude':'Широта отправления','pickup_longitude':'Долгота отправления','stop':'Промежуточные остановки',
    'destination_address':'Пункт назначения','destination_latitude':'Широта назначения','destination_longitude':'Долгота назначения','route':'Маршрут',
    'passenger_count':'Количество пассажиров','trip_purpose':'Цель поездки','created_at':'Дата заявки','scheduled_at':'Время подачи','assigned_at':'Дата назначения',
    'accepted_at':'Подтверждение водителя','driver_departed_at':'Время выезда','driver_arrived_at':'Время прибытия','trip_started_at':'Начало поездки',
    'trip_completed_at':'Окончание поездки','cancelled_at':'Дата отмены','cancellation_reason':'Причина отмены','trip_status':'Статус',
    'actual_distance_km':'Расстояние (км)','estimated_distance_km':'Расстояние расчётное (км)','trip_duration_minutes':'Длительность (мин)',
    'waiting_duration_minutes':'Ожидание (мин)','cost_center':'Центр затрат','created_by_user_id':'Код автора заявки',
    'full_name':'ФИО','employee_number':'Табельный номер','department':'Подразделение','assigned_vehicle_id':'Код закреплённого автомобиля',
    'shift_status':'Статус смены','availability':'Доступность','current_order_id':'Код текущей заявки','phone':'Телефон','email':'Электронная почта',
    'license_plate':'Госномер','make':'Марка','model':'Модель','passenger_capacity':'Количество мест','assigned_driver_id':'Код закреплённого водителя',
    'status':'Статус автомобиля','current_latitude':'Широта местоположения','current_longitude':'Долгота местоположения','location_timestamp':'Время получения координат','garage':'Место стоянки','active_order_id':'Код активной заявки',
    'stop_id':'Код остановки','stop_sequence':'Номер остановки','intermediate_stop':'Промежуточная остановка',
    'trip_event_id':'Код события','event_type':'Тип события','occurred_at':'Дата и время события','actor_user_id':'Код пользователя',
    'actor_name':'Пользователь','reason':'Причина',
}

HEADERS.update({'latitude':'\u0428\u0438\u0440\u043e\u0442\u0430','longitude':'\u0414\u043e\u043b\u0433\u043e\u0442\u0430'})
HEADERS.update({'assignment_method':'\u0421\u043f\u043e\u0441\u043e\u0431 \u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u044f','assignment_distance_km':'\u0420\u0430\u0441\u0441\u0442\u043e\u044f\u043d\u0438\u0435 \u0434\u043e \u043f\u043e\u0434\u0430\u0447\u0438 (\u043a\u043c)'})

def status_label(value):
    if not value: return value
    status=str(value).strip().casefold()
    if status in {'ожидает обработки','ожидает назначения','ожидает распределения','создана'}: return 'Ожидает назначения'
    if status in {'назначена машина','назначена','водитель принял заказ','водитель подтвердил заказ'}: return 'Назначена'
    if status in {'водитель в пути','водитель направляется к вам','водитель на месте','водитель прибыл','поездка началась','в пути'}: return 'В пути'
    if status in {'завершена','завершено'}: return 'Завершена'
    if status in {'отменена','отменено'}: return 'Отменена'
    return value

def shift_label(value):
    return {'active':'На смене','off':'Не на смене','break':'Перерыв'}.get(value,value)

def availability_label(value):
    return {'active':'Свободен на смене','assigned':'Назначена','busy':'В поездке','break':'Перерыв','offline':'Вне смены'}.get(value,value)

def vehicle_status_label(value):
    return {'available':'Свободен','assigned':'Назначен','busy':'Занят','break':'Перерыв','offline':'Офлайн','maintenance':'На обслуживании'}.get(value,value)

def to_local_datetime(value):
    if not value: return None
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=ZoneInfo('UTC'))
        return dt.astimezone(REPORT_TZ).replace(tzinfo=None)
    except (ValueError,TypeError): return None

def format_report_datetime(value):
    dt=to_local_datetime(value)
    return dt.strftime('%d.%m.%Y %H') if dt else None

def filtered_orders(data,date_from:date|None=None,date_to:date|None=None,driver_id:str|None=None,vehicle_id:str|None=None,employee_id:str|None=None,status:str|None=None,department:str|None=None):
    result=[]
    for order in data.get('orders',[]):
        created=to_local_datetime(order.get('created_at'))
        if date_from and (not created or created.date()<date_from): continue
        if date_to and (not created or created.date()>date_to): continue
        if driver_id and order.get('driver_id')!=driver_id: continue
        if vehicle_id and order.get('vehicle_id')!=vehicle_id: continue
        if employee_id and order.get('employee_id')!=employee_id: continue
        if status and status_label(order.get('trip_status') or order.get('status'))!=status_label(status): continue
        if department and order.get('employee_department')!=department: continue
        result.append(order)
    return sorted(result,key=lambda o:o.get('created_at') or '',reverse=True)

def trip_row(o):
    row={column:o.get(column) for column in TRIP_COLUMNS}
    row.update(order_id=o.get('order_id') or o.get('id'),employee_id=o.get('employee_id'),employee_name=o.get('employee_name'),driver_id=o.get('driver_id'),vehicle_id=o.get('vehicle_id'),created_at=o.get('created_at'))
    row['trip_status']=status_label(o.get('trip_status') or o.get('status'))
    stops=o.get('stops') or o.get('stop') or []
    if isinstance(stops,(str,dict)): stops=[stops]
    stop_names=[point.get('address') if isinstance(point,dict) else point for point in stops]
    row['stop']='; '.join(str(point) for point in stop_names if point) or None
    row['route']=' ? '.join(str(point) for point in (o.get('pickup_address') or o.get('pickup'),*stop_names,o.get('destination_address') or o.get('destination')) if point)
    row['assignment_method']={'automatic':'\u0410\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438','manual':'\u0412\u0440\u0443\u0447\u043d\u0443\u044e'}.get(o.get('assignment_method'),o.get('assignment_method'))
    return row

def normalized_datasets(data,orders):
    users=data.get('users',[]); vehicles=data.get('vehicles',[]); shifts=data.get('shifts',{})
    drivers=[{'driver_id':u['id'],'full_name':u.get('name'),'employee_number':u.get('employee_number'),'department':u.get('department'),'assigned_vehicle_id':u.get('assigned_vehicle_id') or u.get('vehicle_id'),'shift_status':shift_label(u.get('shift_status') or shifts.get(u['id'],{}).get('status')),'availability':availability_label(u.get('availability')),'current_order_id':u.get('current_order_id'),'phone':u.get('phone'),'email':u.get('email')} for u in users if u.get('role')=='driver']
    vehicle_rows=[{'vehicle_id':v['id'],'license_plate':v.get('plate'),'make':v.get('make'),'model':v.get('model'),'vehicle_class':v.get('class'),'passenger_capacity':v.get('capacity'),'assigned_driver_id':v.get('driver_id'),'status':vehicle_status_label(v.get('status')),'current_latitude':v.get('current_latitude'),'current_longitude':v.get('current_longitude'),'location_timestamp':v.get('location_updated_at'),'garage':v.get('garage'),'active_order_id':v.get('active_order_id')} for v in vehicles]
    employees=[{'employee_id':u['id'],'full_name':u.get('name'),'employee_number':u.get('employee_number'),'department':u.get('department')} for u in users if u.get('role')=='employee']
    stops=[]; events=[]
    for o in orders:
        raw_stops=o.get('stops') or o.get('stop')
        if isinstance(raw_stops,str): raw_stops=[raw_stops] if raw_stops.strip() else []
        if isinstance(raw_stops,dict): raw_stops=[raw_stops]
        for index,stop in enumerate(raw_stops or [],1):
            label=stop.get('address') or stop.get('name') or stop.get('location') if isinstance(stop,dict) else stop
            if label: stops.append({'stop_id':f"{o.get('id')}-{index}",'order_id':o.get('id'),'stop_sequence':index,'intermediate_stop':label,'latitude':stop.get('latitude') if isinstance(stop,dict) else None,'longitude':stop.get('longitude') if isinstance(stop,dict) else None})
        for index,item in enumerate(o.get('timeline',[])):
            status=status_label(item.get('status'))
            events.append({'trip_event_id':f"{o.get('id')}-{index+1}",'order_id':o.get('id'),'event_type':status or 'Событие','trip_status':status,'occurred_at':item.get('at'),'actor_user_id':item.get('actor_id'),'actor_name':item.get('actor'),'reason':item.get('reason')})
    return {'Поездки':[trip_row(o) for o in orders],'Водители':drivers,'Автомобили':vehicle_rows,'Сотрудники':employees,'Остановки':stops,'История событий':events}

def _safe_cell(value,column):
    if value is None: return None
    if column in TIME_COLUMNS and isinstance(value,str): return to_local_datetime(value)
    if column in {'trip_status','event_type'}: value=status_label(value)
    if isinstance(value,str) and value.startswith(('=','+','-','@')): return "'"+value
    return value

def xlsx_bytes(datasets):
    book=Workbook(); book.remove(book.active); book.properties.title='Отчёт по корпоративным поездкам'; book.properties.subject='Отчёт Qarmet Corporate Taxi'
    for name,columns in DATASET_COLUMNS.items():
        sheet=book.create_sheet(name); rows=datasets.get(name,[])
        sheet.append([HEADERS[column] for column in columns])
        for row in rows: sheet.append([_safe_cell(row.get(column),column) for column in columns])
        sheet.freeze_panes='A2'; sheet.auto_filter.ref=sheet.dimensions
        for idx,column in enumerate(columns,1):
            if column in TIME_COLUMNS:
                for cells in sheet.iter_cols(min_col=idx,max_col=idx,min_row=2):
                    for cell in cells:
                        if isinstance(cell.value,datetime): cell.number_format='dd.mm.yyyy hh'
            sheet.column_dimensions[get_column_letter(idx)].width=min(max(len(HEADERS[column])+2,14),34)
    stream=io.BytesIO(); book.save(stream); return stream.getvalue()

def csv_zip_bytes(datasets):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,columns in DATASET_COLUMNS.items():
            text=io.StringIO(newline=''); writer=csv.writer(text); writer.writerow([HEADERS[column] for column in columns])
            for row in datasets.get(name,[]):
                values=[]
                for column in columns:
                    value=row.get(column)
                    if column in TIME_COLUMNS and value is not None: value=format_report_datetime(value)
                    elif column in {'trip_status','event_type'}: value=status_label(value)
                    if isinstance(value,str) and value.startswith(('=','+','-','@')): value="'"+value
                    values.append(value)
                writer.writerow(values)
            archive.writestr(CSV_FILENAMES[name],text.getvalue().encode('utf-8-sig'))
    return stream.getvalue()

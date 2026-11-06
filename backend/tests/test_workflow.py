import tempfile, unittest
from fastapi.testclient import TestClient
from app import main

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); main.DATA=main.Path(self.tmp.name)/'db.json'
        self.client=TestClient(main.app); self.tokens={}
        for role in ('employee','dispatcher','driver'):
            self.tokens[role]=self.client.post('/api/auth/demo',json={'role':role}).json()['token']
    def tearDown(self): self.client.close(); self.tmp.cleanup()
    def headers(self,role): return {'Authorization':'Bearer '+self.tokens[role]}
    def test_complete_trip_and_persistence(self):
        employee=self.headers('employee'); dispatcher=self.headers('dispatcher'); driver=self.headers('driver')
        profile=self.client.get('/api/me',headers=employee).json()
        self.assertEqual(profile['employee_number'],'QRM-EMP-001')
        self.assertEqual(profile['department'],'Производственный персонал')
        order=self.client.post('/api/orders',headers=employee,json={'pickup':'Офис Qarmet','destination':'Вокзал','pickup_latitude':49.8047,'pickup_longitude':73.1094,'destination_latitude':49.9,'destination_longitude':73.2,'passengers':2,'scheduled_at':'2030-01-01T10:00:00+00:00'}).json()
        self.assertEqual(order['status'],'Ожидает обработки')
        dispatcher_notifications=self.client.get('/api/notifications',headers=dispatcher).json()
        self.assertEqual(dispatcher_notifications[0]['title'],'Новая заявка на поездку')
        self.assertIn('Офис Qarmet',dispatcher_notifications[0]['message'])
        self.assertIn('2030-01-01T10:00:00+00:00',dispatcher_notifications[0]['message'])
        for _ in range(3): self.client.get('/api/notifications',headers=dispatcher).raise_for_status()
        self.assertEqual(len(self.client.get('/api/notifications',headers=dispatcher).json()),1)
        self.assertEqual(self.client.get('/api/notifications',headers=driver).json(),[])
        self.client.post('/api/driver/shift/start',headers=driver).raise_for_status()
        self.assertEqual(self.client.post(f"/api/orders/{order['id']}/assign",headers=dispatcher).status_code,200)
        driver_notification=self.client.get('/api/notifications',headers=driver).json()[0]
        self.assertEqual(driver_notification['title'],'Вам назначена новая поездка')
        self.assertIn('Офис Qarmet',driver_notification['message'])
        self.assertIn('2030-01-01T10:00:00+00:00',driver_notification['message'])
        employee_assignment=self.client.get('/api/notifications',headers=employee).json()[0]
        self.assertEqual(employee_assignment['title'],'Ваша машина назначена')
        self.assertIn('Toyota Camry',employee_assignment['message'])
        # Notification identifiers cannot be used to read another user's record.
        self.assertEqual(self.client.patch(f"/api/notifications/{driver_notification['id']}/read",headers=employee).status_code,404)
        self.assertEqual(self.client.get('/api/notifications/unread-count',headers=driver).json()['count'],1)
        employee_order=self.client.get('/api/bootstrap',headers=employee).json()['orders'][0]
        self.assertEqual(employee_order['vehicle_id'],'car-1')
        self.assertEqual(employee_order['driver_id'],'driver-1')
        self.assertEqual(self.client.post(f"/api/orders/{order['id']}/assign",headers=dispatcher).status_code,409)
        expected_titles={'accept':'Водитель принял заказ','depart':'Водитель направляется к вам','arrive':'Водитель прибыл'}
        for action in ('accept','depart','arrive'):
            response=self.client.post(f"/api/orders/{order['id']}/transition",headers=driver,json={'action':action})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(self.client.get('/api/orders',headers=employee).json()[0]['status'],response.json()['status'])
            newest=self.client.get('/api/notifications',headers=employee).json()[0]
            self.assertEqual(newest['title'],expected_titles[action])
            if action=='arrive': self.assertIn('Офис Qarmet',newest['message'])
        self.assertEqual(self.client.post(f"/api/orders/{order['id']}/transition",headers=driver,json={'action':'start'}).status_code,200)
        self.assertEqual(self.client.get('/api/notifications',headers=dispatcher).json()[0]['title'],'Поездка началась')
        self.assertEqual(next(v for v in self.client.get('/api/vehicles',headers=dispatcher).json() if v['id']=='car-1')['status'],'busy')
        self.assertEqual(self.client.post(f"/api/orders/{order['id']}/transition",headers=driver,json={'action':'complete'}).status_code,200)
        self.assertEqual(self.client.get('/api/orders',headers=employee).json()[0]['status'],'Завершена')
        for role in ('employee','driver','dispatcher'):
            history=self.client.get('/api/orders',headers=self.headers(role)).json()
            self.assertEqual(history[0]['status'],'Завершена')
        titles=[n['title'] for n in self.client.get('/api/notifications',headers=employee).json()]
        self.assertIn('Поездка завершена',titles)
        dispatcher_titles=[n['title'] for n in self.client.get('/api/notifications',headers=dispatcher).json()]
        self.assertIn('Заказ завершён',dispatcher_titles)
        self.client.patch(f"/api/notifications/{employee_assignment['id']}/read",headers=employee).raise_for_status()
        self.assertEqual(self.client.get('/api/notifications/unread-count',headers=employee).json()['count'],len(titles)-1)
        self.client.post('/api/notifications/read-all',headers=dispatcher).raise_for_status()
        self.assertEqual(self.client.get('/api/notifications/unread-count',headers=dispatcher).json()['count'],0)
        persisted=main.read(); self.assertEqual(len(persisted['orders']),1); self.assertEqual(next(v for v in persisted['vehicles'] if v['id']=='car-1')['status'],'available')
        self.assertTrue(persisted['notifications'])
        with TestClient(main.app) as restarted_backend:
            restored=restarted_backend.get('/api/notifications',headers=employee).json()
            self.assertEqual(len(restored),len(titles))
    def test_busy_queue_and_role_access(self):
        emp=self.headers('employee'); dsp=self.headers('dispatcher'); drv=self.headers('driver')
        a=self.client.post('/api/orders',headers=emp,json={'pickup':'A','destination':'B','pickup_latitude':49.8,'pickup_longitude':73.1,'destination_latitude':49.9,'destination_longitude':73.2}).json()
        self.client.post('/api/driver/shift/start',headers=drv)
        self.client.post(f"/api/orders/{a['id']}/assign",headers=dsp).raise_for_status()
        b=self.client.post('/api/orders',headers=emp,json={'pickup':'B','destination':'C','pickup_latitude':49.9,'pickup_longitude':73.2,'destination_latitude':50.0,'destination_longitude':73.3}).json()
        self.assertEqual(self.client.post(f"/api/orders/{b['id']}/assign",headers=dsp).status_code,409)
        self.assertEqual(self.client.post(f"/api/orders/{a['id']}/transition",headers=emp,json={'action':'accept'}).status_code,403)
        for action in ('accept','depart','arrive','start','complete'):
            response=self.client.post(f"/api/orders/{a['id']}/transition",headers=drv,json={'action':action})
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.post(f"/api/orders/{b['id']}/assign",headers=dsp).status_code,200)
    def test_automatic_fifo_queue_multistop_and_report_labels(self):
        employee=self.headers('employee'); dispatcher=self.headers('dispatcher'); driver=self.headers('driver')
        self.client.post('/api/driver/shift/start',headers=driver).raise_for_status()
        setting=self.client.patch('/api/settings/dispatch',headers=dispatcher,json={'mode':'automatic'})
        self.assertEqual(setting.status_code,200,setting.text)
        second=self.client.post('/api/auth/demo',json={'role':'driver','user_id':'driver-2'}).json()
        second_driver={'Authorization':'Bearer '+second['token']}
        self.client.post('/api/driver/shift/start',headers=second_driver).raise_for_status()
        fleet_data=main.read()
        near=next(v for v in fleet_data['vehicles'] if v['id']=='car-1'); near.update(current_latitude=49.8,current_longitude=73.1,location_updated_at=main.now(),location_source='browser_gps')
        far=next(v for v in fleet_data['vehicles'] if v['id']=='vehicle-02'); far.update(current_latitude=50.8,current_longitude=74.1,location_updated_at=main.now(),location_source='browser_gps')
        ranked=main.VehicleAssignmentService.recommend(fleet_data,{'pickup_latitude':49.8,'pickup_longitude':73.1})
        self.assertEqual(ranked[0]['id'],'car-1')
        far['status']='offline'
        main.write(fleet_data)
        payload={'pickup':'A','destination':'B','pickup_latitude':49.8,'pickup_longitude':73.1,'destination_latitude':49.9,'destination_longitude':73.2,'stops':[{'address':'Stop 1','latitude':49.85,'longitude':73.15},{'address':'Stop 2','latitude':49.87,'longitude':73.17}]}
        first=self.client.post('/api/orders',headers=employee,json=payload)
        self.assertEqual(first.status_code,201,first.text)
        first_order=first.json(); self.assertEqual(first_order['assignment_method'],'automatic'); self.assertEqual(first_order['vehicle_id'],'car-1')
        self.assertEqual([p['address'] for p in first_order['stops']],['Stop 1','Stop 2'])
        queued=self.client.post('/api/orders',headers=employee,json={'pickup':'A','destination':'B','pickup_latitude':49.7,'pickup_longitude':73.0,'destination_latitude':49.95,'destination_longitude':73.3})
        self.assertEqual(queued.status_code,201,queued.text); queued_order=queued.json()
        self.assertIsNone(queued_order.get('vehicle_id'))
        self.assertEqual(len(self.client.get(f"/api/orders/{queued_order['id']}/eligible-vehicles",headers=dispatcher).json()),0)
        for action in ('accept','depart','arrive','start','complete'):
            response=self.client.post(f"/api/orders/{first_order['id']}/transition",headers=driver,json={'action':action})
            self.assertEqual(response.status_code,200,response.text)
        orders=self.client.get('/api/orders',headers=employee).json()
        released=next(o for o in orders if o['id']==queued_order['id'])
        self.assertEqual(released['assignment_method'],'automatic')
        self.assertIsNotNone(released['vehicle_id'])
        self.assertEqual(next(o for o in orders if o['id']==first_order['id'])['stops'][1]['address'],'Stop 2')
        datasets=self.client.get('/api/reports/summary',headers=dispatcher).json()
        self.assertIn('assignment_method',datasets['trips'][0])
        xlsx=self.client.get('/api/reports/export/xlsx',headers=dispatcher)
        self.assertEqual(xlsx.status_code,200)
        from openpyxl import load_workbook
        from io import BytesIO
        book=load_workbook(BytesIO(xlsx.content),read_only=True)
        self.assertIn('\u041e\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0438',book.sheetnames)
        self.assertIn('\u0421\u043f\u043e\u0441\u043e\u0431 \u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u044f',list(next(book['\u041f\u043e\u0435\u0437\u0434\u043a\u0438'].iter_rows(values_only=True))))
        self.assertEqual([r[3] for r in list(book['\u041e\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0438'].iter_rows(min_row=2,values_only=True))[:2]],['Stop 1','Stop 2'])

    def test_logout_invalidates_session_and_login_works_again(self):
        employee=self.headers('employee')
        self.assertEqual(self.client.post('/api/auth/logout',headers=employee).status_code,200)
        self.assertEqual(self.client.get('/api/me',headers=employee).status_code,401)
        new_login=self.client.post('/api/auth/demo',json={'role':'driver'})
        self.assertEqual(new_login.status_code,200)
        self.assertEqual(self.client.get('/api/me',headers={'Authorization':'Bearer '+new_login.json()['token']}).json()['role'],'driver')
    def test_incident_notifies_dispatcher_as_high_priority(self):
        result=self.client.post('/api/incidents',headers=self.headers('driver'),json={'kind':'sos','message':'Требуется помощь'})
        self.assertEqual(result.status_code,201)
        notification=self.client.get('/api/notifications',headers=self.headers('dispatcher')).json()[0]
        self.assertEqual(notification['priority'],'high')
        self.assertEqual(notification['title'],'Экстренный сигнал SOS')
    def test_notification_feed_is_private_even_between_employees(self):
        d=main.read(); second={'id':'employee-test-2','name':'Сотрудник теста','role':'employee','employee_number':'TEST-002'}
        d['users'].append(second); d['sessions']['second-employee-token']=second['id']
        main.create_notification(d,second['id'],None,'private_test','Только для второго сотрудника','Личное сообщение')
        main.write(d)
        second_headers={'Authorization':'Bearer second-employee-token'}
        self.assertEqual(self.client.get('/api/notifications',headers=self.headers('employee')).json(),[])
        self.assertEqual(len(self.client.get('/api/notifications',headers=second_headers).json()),1)
        self.client.post('/api/notifications/read-all',headers=self.headers('employee')).raise_for_status()
        self.assertEqual(self.client.get('/api/notifications/unread-count',headers=second_headers).json()['count'],1)
    def test_invalid_routes_and_cancel_reason(self):
        h=self.headers('employee')
        self.assertEqual(self.client.post('/api/orders',headers=h,json={'pickup':'A','destination':'A'}).status_code,422)
        o=self.client.post('/api/orders',headers=h,json={'pickup':'A','destination':'B','pickup_latitude':49.8,'pickup_longitude':73.1,'destination_latitude':49.9,'destination_longitude':73.2}).json()
        self.assertEqual(self.client.post(f"/api/orders/{o['id']}/cancel",headers=h,json={'reason':''}).status_code,422)
        self.assertEqual(self.client.post(f"/api/orders/{o['id']}/cancel",headers=h,json={'reason':'Изменились планы'}).status_code,200)
        dispatcher_notifications=self.client.get('/api/notifications',headers=self.headers('dispatcher')).json()
        cancellation=next(n for n in dispatcher_notifications if n['type']=='order_cancelled')
        self.assertIn('Изменились планы',cancellation['message'])

    def test_demo_fleet_and_manual_eligible_assignment(self):
        dispatcher=self.headers('dispatcher')
        fleet=self.client.get('/api/vehicles',headers=dispatcher).json()
        drivers=self.client.get('/api/drivers',headers=dispatcher).json()
        self.assertEqual(len(fleet),15)
        self.assertEqual(len(drivers),15)
        self.assertEqual(len({(v['current_latitude'],v['current_longitude']) for v in fleet}),15)
        self.assertTrue(all(v['location_source']=='demo' for v in fleet))
        employee=self.headers('employee')
        order=self.client.post('/api/orders',headers=employee,json={'pickup':'A','destination':'B','pickup_latitude':49.8,'pickup_longitude':73.1,'destination_latitude':49.9,'destination_longitude':73.2,'passengers':8}).json()
        driver_login=self.client.post('/api/auth/demo',json={'role':'driver','user_id':'driver-7'})
        self.assertEqual(driver_login.status_code,200)
        driver_headers={'Authorization':'Bearer '+driver_login.json()['token']}
        self.client.post('/api/driver/shift/start',headers=driver_headers).raise_for_status()
        d=main.read(); vehicle=next(v for v in d['vehicles'] if v['id']=='vehicle-07')
        vehicle.update(current_latitude=49.81,current_longitude=73.11,location_updated_at=main.now(),location_source='browser_gps')
        main.write(d)
        self.assertEqual(next(v for v in main.read()['vehicles'] if v['id']=='vehicle-07')['current_latitude'],49.81)
        eligible=self.client.get(f"/api/orders/{order['id']}/eligible-vehicles",headers=dispatcher).json()
        self.assertEqual([v['id'] for v in eligible],['vehicle-07'])
        self.assertTrue(eligible[0]['location_fresh'])
        self.assertIsNotNone(eligible[0]['approximate_distance_km'])
        assigned=self.client.post(f"/api/orders/{order['id']}/assign",headers=dispatcher,json={'vehicle_id':'vehicle-07'})
        self.assertEqual(assigned.status_code,200,assigned.text)
        self.assertEqual(assigned.json()['driver_id'],'driver-7')
        for action in ('accept','depart','arrive','start'):
            self.client.post(f"/api/orders/{order['id']}/transition",headers=driver_headers,json={'action':action}).raise_for_status()
        self.assertEqual(next(v for v in self.client.get('/api/vehicles',headers=dispatcher).json() if v['id']=='vehicle-07')['status'],'busy')

    def test_reports_exports_are_structured_and_dispatcher_only(self):
        employee=self.headers('employee'); dispatcher=self.headers('dispatcher')
        order=self.client.post('/api/orders',headers=employee,json={'pickup':'A','destination':'B','pickup_latitude':49.8,'pickup_longitude':73.1,'destination_latitude':49.9,'destination_longitude':73.2,'cost_center':'CC-01'}).json()
        summary=self.client.get('/api/reports/summary',headers=dispatcher).json()
        self.assertEqual(summary['total_trips'],1)
        self.assertEqual(summary['trips'][0]['cost_center'],'CC-01')
        self.assertEqual(summary['filters']['statuses'],['Ожидает назначения','Назначена','В пути','Завершена','Отменена'])
        xlsx=self.client.get('/api/reports/export/xlsx',headers=dispatcher)
        self.assertEqual(xlsx.status_code,200)
        from io import BytesIO
        from openpyxl import load_workbook
        workbook=load_workbook(BytesIO(xlsx.content),read_only=True)
        self.assertEqual(workbook.sheetnames,['Поездки','Водители','Автомобили','Сотрудники','Остановки','История событий'])
        self.assertEqual([cell.value for cell in workbook['Поездки'][1]][:4],['Номер заявки','Код сотрудника','Сотрудник','Подразделение'])
        self.assertEqual(workbook['Поездки']['A2'].value,order['id'])
        trip_headers=[cell.value for cell in workbook['Поездки'][1]]
        created_column=trip_headers.index('Дата заявки')+1
        status_column=trip_headers.index('Статус')+1
        self.assertEqual(workbook['Поездки'].cell(2,created_column).number_format,'dd.mm.yyyy hh')
        self.assertEqual(workbook['Поездки'].cell(2,status_column).value,'Ожидает назначения')
        archive=self.client.get('/api/reports/export/csv',headers=dispatcher)
        self.assertEqual(archive.status_code,200)
        import zipfile
        with zipfile.ZipFile(BytesIO(archive.content)) as zipped:
            self.assertEqual(set(zipped.namelist()),{'Поездки.csv','Водители.csv','Автомобили.csv','Сотрудники.csv','Остановки.csv','История_событий.csv'})
            trips_csv=zipped.read('Поездки.csv')
            self.assertTrue(trips_csv.startswith(b'\xef\xbb\xbf'))
            self.assertIn('Номер заявки'.encode('utf-8'),trips_csv)
            self.assertIn('Ожидает назначения'.encode('utf-8'),trips_csv)
            import csv
            rows=list(csv.reader(trips_csv.decode('utf-8-sig').splitlines()))
            self.assertRegex(rows[1][rows[0].index('Дата заявки')],r'^\d{2}\.\d{2}\.\d{4} \d{2}$')
        self.assertEqual(self.client.get('/api/reports/summary',headers=employee).status_code,403)

if __name__=='__main__': unittest.main()

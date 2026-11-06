"""Replaceable vehicle assignment ranking and hard eligibility rules."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from math import asin, cos, radians, sin, sqrt

GPS_FRESHNESS = timedelta(minutes=20)

def haversine_km(lat1:float,lon1:float,lat2:float,lon2:float)->float:
    """Approximate straight-line distance; this is not road distance or ETA."""
    earth_radius_km=6371.0088
    dlat=radians(lat2-lat1); dlon=radians(lon2-lon1)
    a=sin(dlat/2)**2+cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return 2*earth_radius_km*asin(sqrt(a))

def location_is_fresh(vehicle:dict,now:datetime|None=None)->bool:
    lat=vehicle.get('current_latitude'); lon=vehicle.get('current_longitude'); stamp=vehicle.get('location_updated_at')
    if lat is None or lon is None or not stamp: return False
    try:
        observed=datetime.fromisoformat(stamp.replace('Z','+00:00'))
        if observed.tzinfo is None: return False
        return (now or datetime.now(timezone.utc))-observed <= GPS_FRESHNESS
    except (ValueError,TypeError): return False

def vehicle_position(vehicle:dict,now:datetime|None=None)->dict:
    """Return the usable map position without ever disguising GPS as demo data."""
    if location_is_fresh(vehicle,now) and vehicle.get('location_source') in ('browser_gps','gps'):
        return {'latitude':vehicle['current_latitude'],'longitude':vehicle['current_longitude'],'source':'gps','updated_at':vehicle.get('location_updated_at'),'fresh':True}
    latitude=vehicle.get('demo_latitude'); longitude=vehicle.get('demo_longitude')
    if latitude is None or longitude is None:
        if vehicle.get('location_source')=='demo':
            latitude=vehicle.get('current_latitude'); longitude=vehicle.get('current_longitude')
    if latitude is not None and longitude is not None:
        return {'latitude':latitude,'longitude':longitude,'source':'demo','updated_at':vehicle.get('demo_location_updated_at'),'fresh':False}
    return {'latitude':None,'longitude':None,'source':None,'updated_at':None,'fresh':False}

class VehicleAssignmentService:
    """Shared eligibility and proximity ranking for manual and automatic assignment."""
    automatic_assignment_enabled=False

    @staticmethod
    def eligible_vehicles(data:dict,order:dict)->list[dict]:
        drivers={u['id']:u for u in data.get('users',[]) if u.get('role')=='driver'}
        shifts=data.get('shifts',{})
        active_order_driver_ids={o.get('driver_id') for o in data.get('orders',[]) if o.get('status') in ('Назначена машина','Водитель принял заказ','Водитель в пути','Водитель на месте','Поездка началась')}
        eligible=[]
        for v in data.get('vehicles',[]):
            driver=drivers.get(v.get('driver_id'))
            if not driver or v.get('status')!='available': continue
            if shifts.get(driver['id'],{}).get('status')!='active': continue
            if driver.get('availability') not in (None,'active','available'): continue
            if driver['id'] in active_order_driver_ids: continue
            if v.get('active_order_id'): continue
            if v.get('capacity',0)<order.get('passengers',1): continue
            requested=order.get('vehicle_class')
            if requested and requested not in ('Стандарт','Любой') and v.get('class')!=requested: continue
            position=vehicle_position(v)
            eligible.append({**v,'driver':driver,'location_fresh':position['fresh'],'location_source':position['source'],'position_latitude':position['latitude'],'position_longitude':position['longitude'],'position_updated_at':position['updated_at']})
        return eligible

    @staticmethod
    def recommend(data:dict,order:dict,pickup_latitude:float|None=None,pickup_longitude:float|None=None)->list[dict]:
        lat=pickup_latitude if pickup_latitude is not None else order.get('pickup_latitude')
        lon=pickup_longitude if pickup_longitude is not None else order.get('pickup_longitude')
        results=[]
        for v in VehicleAssignmentService.eligible_vehicles(data,order):
            distance=None
            if lat is not None and lon is not None and v['position_latitude'] is not None:
                distance=haversine_km(lat,lon,v['position_latitude'],v['position_longitude'])
            results.append({**v,'approximate_distance_km':round(distance,2) if distance is not None else None,'recommendation_basis':'fresh_gps' if distance is not None and v['location_source']=='gps' else 'demo_location' if distance is not None else 'distance_unavailable'})
        results.sort(key=lambda v:(v['approximate_distance_km'] is None,v['approximate_distance_km'] if v['approximate_distance_km'] is not None else float('inf'),v['id']))
        return results

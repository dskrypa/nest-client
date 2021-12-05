MINIMUM_TEMPERATURE_F = 50
MAXIMUM_TEMPERATURE_F = 90
MINIMUM_TEMPERATURE_C = 9
MAXIMUM_TEMPERATURE_C = 32

JWT_URL = 'https://nestauthproxyservice-pa.googleapis.com/v1/issue_jwt'
NEST_API_KEY = 'AIzaSyAdkSIMNc51XGNEAYWasX9UOWkS5P6sZE4'  # public key from Nest's website
NEST_URL = 'https://home.nest.com'
OAUTH_URL = 'https://accounts.google.com/o/oauth2/iframerpc'

TARGET_TEMP_TYPES = {'cool', 'heat', 'range', 'off'}

NEST_WHERE_MAP = {
    '00000000-0000-0000-0000-000100000000': 'Entryway',
    '00000000-0000-0000-0000-000100000001': 'Basement',
    '00000000-0000-0000-0000-000100000002': 'Hallway',
    '00000000-0000-0000-0000-000100000003': 'Den',
    '00000000-0000-0000-0000-000100000004': 'Attic',
    '00000000-0000-0000-0000-000100000005': 'Master Bedroom',
    '00000000-0000-0000-0000-000100000006': 'Downstairs',
    '00000000-0000-0000-0000-000100000007': 'Garage',
    '00000000-0000-0000-0000-000100000008': 'Kids Room',
    '00000000-0000-0000-0000-000100000009': 'Bathroom',
    '00000000-0000-0000-0000-00010000000a': 'Kitchen',
    '00000000-0000-0000-0000-00010000000b': 'Family Room',
    '00000000-0000-0000-0000-00010000000c': 'Living Room',
    '00000000-0000-0000-0000-00010000000d': 'Bedroom',
    '00000000-0000-0000-0000-00010000000e': 'Office',
    '00000000-0000-0000-0000-00010000000f': 'Upstairs',
    '00000000-0000-0000-0000-000100000010': 'Dining Room',
    '00000000-0000-0000-0000-000100000011': 'Backyard',
    '00000000-0000-0000-0000-000100000012': 'Driveway',
    '00000000-0000-0000-0000-000100000013': 'Front Yard',
    '00000000-0000-0000-0000-000100000014': 'Outside',
    '00000000-0000-0000-0000-000100000015': 'Guest House',
    '00000000-0000-0000-0000-000100000016': 'Shed',
    '00000000-0000-0000-0000-000100000017': 'Deck',
    '00000000-0000-0000-0000-000100000018': 'Patio',
    '00000000-0000-0000-0000-00010000001a': 'Guest Room',
    '00000000-0000-0000-0000-00010000001b': 'Front Door',
    '00000000-0000-0000-0000-00010000001c': 'Side Door',
    '00000000-0000-0000-0000-00010000001d': 'Back Door'
}

USER_CHILD_TYPES = {'buckets': False, 'message_center': False, 'user_alert_dialog': False, 'user_settings': False}
STRUCTURE_CHILD_TYPES = {
    'geofence_info': False,
    'partner_programs': False,
    'safety': False,
    'safety_summary': False,
    'structure_history': False,
    'structure_metadata': False,
    'trip': False,
    'utility': False,
    'where': False,
    'wwn_security': False
}
DEVICE_CHILD_TYPES = {
    'cloud_algo': False,
    'demand_charge': False,
    'demand_response': False,
    'demand_response_fleet': False,
    'device_alert_dialog': False,
    'device_migration': False,
    'diagnostics': False,  # Always fails
    'energy_latest': False,
    'energy_weekly': False,
    'found_savings': False,
    'hvac_issues': False,  # Always fails
    'hvac_partner': False,
    'link': False,
    'message': True,
    'metadata': False,
    'rcs_settings': False,
    'schedule': False,
    'shared': True,
    'tou': False,
    'track': False,
    'tuneups': False,
}

BUCKET_CHILD_TYPES = {'user': USER_CHILD_TYPES, 'device': DEVICE_CHILD_TYPES, 'structure': STRUCTURE_CHILD_TYPES}

ALL_BUCKET_TYPES = [
    'buckets',
    'delayed_topaz',
    'demand_response',
    'device',
    'device_alert_dialog',
    'geofence_info',
    'kryptonite',
    'link',
    'message',
    'message_center',
    'metadata',
    'occupancy',
    'quartz',
    'safety',
    'rcs_settings',
    'safety_summary',
    'schedule',
    'shared',
    'structure',
    'structure_history',
    'structure_metadata',
    'topaz',
    'topaz_resource',
    'track',
    'trip',
    'tuneups',
    'user',
    'user_alert_dialog',
    'user_settings',
    'where',
    'widget_track'
]

INIT_BUCKET_TYPES = {'buckets', 'device', 'message', 'schedule', 'shared', 'structure', 'user', 'user_settings'}

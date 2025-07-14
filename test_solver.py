import unittest
import datetime
import math
from collections import Counter

from solver import solve_driver_only_schedule, process_results

def create_base_test_data(num_hours=24):
    """Creates a base dictionary with default values for a test."""
    now = datetime.datetime.now(datetime.UTC)
    availability = {}
    team_members = [
        {"name": "Driver A", "isDriver": True, "isSpotter": False, "preferredStints": 4, "minimumRestHours": 0},
        {"name": "Driver B", "isDriver": True, "isSpotter": False, "preferredStints": 4, "minimumRestHours": 0},
        {"name": "Driver C", "isDriver": True, "isSpotter": False, "preferredStints": 4, "minimumRestHours": 0},
    ]

    for member in team_members:
        availability[member['name']] = {}
        for i in range(num_hours + 2):
            hour_key_date = now + datetime.timedelta(hours=i)
            hour_key_date = hour_key_date.replace(minute=0, second=0, microsecond=0)
            availability_key = hour_key_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
            availability[member['name']][availability_key] = "Available"

    return {
        "durationHours": 6,
        "raceStartUTC": now.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        "avgLapTimeInSeconds": 120,
        "pitTimeInSeconds": 60,
        "fuelTankSize": 100,
        "fuelUsePerLap": 5,
        "teamMembers": team_members,
        "availability": availability
    }

class TestScheduler(unittest.TestCase):

    def test_1_perfect_world_balance(self):
        """Tests that a simple race results in a perfect round-robin."""
        print("\n--- Running Test 1: Perfect World Balance ---")
        data = create_base_test_data()
        data['durationHours'] = 5.5
        
        prob, _, total_stints, _, driver_pool, drive_vars = solve_driver_only_schedule(data, time_limit=60)
        schedule = process_results(prob, total_stints, driver_pool, drive_vars)
        
        self.assertIsNotNone(schedule, "Solver failed to find a solution.")
        drivers = [s['driver'] for s in schedule]
        self.assertEqual(len(drivers), 9)
        counts = Counter(drivers)
        self.assertEqual(counts['Driver A'], 3)
        self.assertEqual(counts['Driver B'], 3)
        self.assertEqual(counts['Driver C'], 3)

    def test_2_availability_constraint(self):
        """Tests that an unavailable driver is never assigned."""
        print("\n--- Running Test 2: Availability Constraint ---")
        data = create_base_test_data()
        data['durationHours'] = 5.5
        
        start_hour_key_date = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
        start_hour_key_date = start_hour_key_date.replace(minute=0, second=0, microsecond=0)
        availability_key = start_hour_key_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        data['availability']['Driver B'][availability_key] = "Unavailable"
        
        prob, _, total_stints, _, driver_pool, drive_vars = solve_driver_only_schedule(data, time_limit=60)
        schedule = process_results(prob, total_stints, driver_pool, drive_vars)
        
        self.assertIsNotNone(schedule, "Solver failed to find a solution.")
        self.assertNotEqual(schedule[0]['driver'], 'Driver B')

    def test_3_fair_share_constraint(self):
        """Tests that the Fair Share rule is enforced."""
        print("\n--- Running Test 3: Fair Share Constraint ---")
        data = create_base_test_data(num_hours=24)
        data['durationHours'] = 24
        
        start_time = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
        for i in range(5): 
            hour_key_date = start_time + datetime.timedelta(hours=i)
            hour_key_date = hour_key_date.replace(minute=0, second=0, microsecond=0)
            availability_key = hour_key_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
            data['availability']['Driver A'][availability_key] = "Preferred"
            data['availability']['Driver B'][availability_key] = "Preferred"

        prob, _, total_stints, _, driver_pool, drive_vars = solve_driver_only_schedule(data, time_limit=60)
        schedule = process_results(prob, total_stints, driver_pool, drive_vars)
        
        self.assertIsNotNone(schedule, "Solver failed to find a solution.")
        counts = Counter(s['driver'] for s in schedule)
        min_required_stints = 3
        self.assertGreaterEqual(counts['Driver C'], min_required_stints, "Driver C was not assigned their fair share of stints.")

    def test_4_max_consecutive_stints(self):
        """Tests that the consecutive stint limit is respected."""
        print("\n--- Running Test 4: Max Consecutive Stints ---")
        data = create_base_test_data()
        data['durationHours'] = 10 
        data['teamMembers'][0]['preferredStints'] = 2
        
        prob, _, total_stints, _, driver_pool, drive_vars = solve_driver_only_schedule(data, time_limit=60)
        schedule = process_results(prob, total_stints, driver_pool, drive_vars)
        
        self.assertIsNotNone(schedule, "Solver failed to find an optimal solution within the time limit.")
        
        drivers = [s['driver'] for s in schedule]
        max_consecutive_found = 0
        current_consecutive = 0
        current_driver = None
        for driver in drivers:
            if driver == current_driver and driver == 'Driver A':
                current_consecutive += 1
            else:
                current_consecutive = 1
            current_driver = driver
            max_consecutive_found = max(max_consecutive_found, current_consecutive)
            
        self.assertLessEqual(max_consecutive_found, 2)

    def test_5_minimum_rest(self):
        """Tests that the minimum rest period is enforced."""
        print("\n--- Running Test 5: Minimum Rest ---")
        data = create_base_test_data(num_hours=24)
        data['durationHours'] = 24
        data['teamMembers'][0]['minimumRestHours'] = 6
        
        prob, data, total_stints, stint_laps, driver_pool, drive_vars = solve_driver_only_schedule(data, time_limit=60)
        schedule = process_results(prob, total_stints, driver_pool, drive_vars)
        
        self.assertIsNotNone(schedule, "Solver failed to find a solution.")
        
        driver_a_stint_indices = [i for i, s in enumerate(schedule) if s['driver'] == 'Driver A']
        
        if len(driver_a_stint_indices) <= 1:
            has_long_rest = True
        else:
            initial_rest = driver_a_stint_indices[0]
            final_rest = (total_stints - 1) - driver_a_stint_indices[-1]
            internal_rests = [driver_a_stint_indices[i+1] - driver_a_stint_indices[i] - 1 for i in range(len(driver_a_stint_indices) - 1)]
            
            max_rest_stints = max([initial_rest] + internal_rests + [final_rest])

            stint_with_pit_seconds = (stint_laps * data['avgLapTimeInSeconds']) + data['pitTimeInSeconds']
            required_rest_stints = 0
            if stint_with_pit_seconds > 0:
                 required_rest_stints = math.floor((data['teamMembers'][0]['minimumRestHours'] * 3600) / stint_with_pit_seconds)

            has_long_rest = (max_rest_stints >= required_rest_stints)

        self.assertTrue(has_long_rest, f"Driver A did not get a minimum {data['teamMembers'][0]['minimumRestHours']}-hour rest period.")

if __name__ == '__main__':
    unittest.main()
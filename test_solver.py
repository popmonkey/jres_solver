import unittest
import json
import datetime
from collections import Counter

# Import the functions to be tested from your main solver script
# Assumes the solver script is named 'pulp_solver.py'
from pulp_solver import solve_driver_only_schedule, process_results

# --- Helper function to create test data ---
def create_base_test_data(num_hours=24):
    """Creates a base dictionary with default values for a test."""
    # FIX: Use the modern, non-deprecated way to get the current UTC time.
    now = datetime.datetime.now(datetime.UTC)
    availability = {}
    team_members = [
        {"name": "Driver A", "role": "Driver Only", "preferredStints": 1, "minimumRestHours": 0},
        {"name": "Driver B", "role": "Driver Only", "preferredStints": 1, "minimumRestHours": 0},
        {"name": "Driver C", "role": "Driver Only", "preferredStints": 1, "minimumRestHours": 0},
    ]

    for member in team_members:
        availability[member['name']] = {}
        for i in range(num_hours):
            hour_key_date = now + datetime.timedelta(hours=i)
            hour_key_date = hour_key_date.replace(minute=0, second=0, microsecond=0)
            availability_key = hour_key_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
            availability[member['name']][availability_key] = "Available"

    return {
        "durationHours": 6,
        "raceStartUTC": now.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        "avgLapTimeInSeconds": 120, # 2 minutes
        "pitTimeInSeconds": 60,
        "fuelTankSize": 100,
        "fuelUsePerLap": 5, # 20 laps per stint
        "teamMembers": team_members,
        "availability": availability
    }

class TestScheduler(unittest.TestCase):

    def test_1_perfect_world_balance(self):
        """Tests that a simple race results in a perfect round-robin."""
        print("\n--- Running Test 1: Perfect World Balance ---")
        data = create_base_test_data()
        # With these params, total_stints = ceil((5.5 * 3600) / ((20 * 120) + 60)) = ceil(8.04) = 9
        data['durationHours'] = 5.5 
        
        prob, _, total_stints, _, driver_pool, drive_vars = solve_driver_only_schedule(data, time_limit=2)
        schedule = process_results(prob, total_stints, driver_pool, drive_vars)
        
        self.assertIsNotNone(schedule, "Solver failed to find a solution.")
        
        drivers = [s['driver'] for s in schedule]
        # FIX: The assertion should check for the correctly calculated number of stints.
        self.assertEqual(len(drivers), 9)
        
        counts = Counter(drivers)
        # FIX: With 9 stints and 3 drivers, each should have 3 stints.
        self.assertEqual(counts['Driver A'], 3)
        self.assertEqual(counts['Driver B'], 3)
        self.assertEqual(counts['Driver C'], 3)

    def test_2_availability_constraint(self):
        """Tests that an unavailable driver is never assigned."""
        print("\n--- Running Test 2: Availability Constraint ---")
        data = create_base_test_data()
        data['durationHours'] = 5.5 # Approx 9 stints
        
        start_hour_key_date = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
        start_hour_key_date = start_hour_key_date.replace(minute=0, second=0, microsecond=0)
        availability_key = start_hour_key_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        data['availability']['Driver B'][availability_key] = "Unavailable"
        
        prob, _, total_stints, _, driver_pool, drive_vars = solve_driver_only_schedule(data, time_limit=2)
        schedule = process_results(prob, total_stints, driver_pool, drive_vars)
        
        self.assertIsNotNone(schedule, "Solver failed to find a solution.")
        
        self.assertNotEqual(schedule[0]['driver'], 'Driver B')

    def test_3_fair_share_constraint(self):
        """Tests that the Fair Share rule is enforced even for a 'less preferred' driver."""
        print("\n--- Running Test 3: Fair Share Constraint ---")
        data = create_base_test_data(num_hours=24)
        data['durationHours'] = 24
        
        # Make Driver C less desirable by having no 'Preferred' slots,
        # while A and B have some. The Fair Share rule should still force C to be used.
        start_time = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
        for i in range(5): # Give A and B some preferred slots
            hour_key_date = start_time + datetime.timedelta(hours=i)
            hour_key_date = hour_key_date.replace(minute=0, second=0, microsecond=0)
            availability_key = hour_key_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
            data['availability']['Driver A'][availability_key] = "Preferred"
            data['availability']['Driver B'][availability_key] = "Preferred"

        prob, _, total_stints, _, driver_pool, drive_vars = solve_driver_only_schedule(data, time_limit=2)
        schedule = process_results(prob, total_stints, driver_pool, drive_vars)
        
        self.assertIsNotNone(schedule, "Solver failed to find a solution.")
        
        counts = Counter(s['driver'] for s in schedule)
        
        # For a 24h race with these params, total_stints = ceil((24*3600)/((20*120)+60)) = ceil(35.2) = 36
        # Fair share is ceil(0.25 * (36/3)) = 3 stints
        min_required_stints = 3
        
        self.assertGreaterEqual(counts['Driver C'], min_required_stints, "Driver C was not assigned their fair share of stints.")

    def test_4_max_consecutive_stints(self):
        """Tests that the consecutive stint limit is respected."""
        print("\n--- Running Test 4: Max Consecutive Stints ---")
        data = create_base_test_data()
        data['durationHours'] = 24
        data['teamMembers'][0]['preferredStints'] = 2 # Driver A prefers max 2 stints
        
        prob, _, total_stints, _, driver_pool, drive_vars = solve_driver_only_schedule(data, time_limit=2)
        schedule = process_results(prob, total_stints, driver_pool, drive_vars)
        
        self.assertIsNotNone(schedule, "Solver failed to find a solution.")
        
        drivers = [s['driver'] for s in schedule]
        max_consecutive_found = 0
        current_consecutive = 0
        for i in range(len(drivers)):
            if i > 0 and drivers[i] == drivers[i-1] and drivers[i] == 'Driver A':
                current_consecutive += 1
            else:
                current_consecutive = 1
            max_consecutive_found = max(max_consecutive_found, current_consecutive)
            
        self.assertLessEqual(max_consecutive_found, 2)

    def test_5_minimum_rest(self):
        """Tests that the minimum rest period is enforced."""
        print("\n--- Running Test 5: Minimum Rest ---")
        data = create_base_test_data()
        data['durationHours'] = 24
        data['teamMembers'][0]['minimumRestHours'] = 6 # Driver A needs a 6-hour break
        
        prob, _, total_stints, _, driver_pool, drive_vars = solve_driver_only_schedule(data, time_limit=2)
        schedule = process_results(prob, total_stints, driver_pool, drive_vars)
        
        self.assertIsNotNone(schedule, "Solver failed to find a solution.")
        
        driver_a_stints = [i for i, s in enumerate(schedule) if s['driver'] == 'Driver A']
        
        has_long_rest = False
        if len(driver_a_stints) > 1:
            for i in range(len(driver_a_stints) - 1):
                stint_gap = driver_a_stints[i+1] - driver_a_stints[i]
                
                # Calculate rest time in hours
                stint_with_pit_seconds = (data['avgLapTimeInSeconds'] * 20) + data['pitTimeInSeconds']
                rest_hours = (stint_gap * stint_with_pit_seconds) / 3600
                
                if rest_hours >= 6:
                    has_long_rest = True
                    break
        else: # If only one stint, the rest of the race is a rest period
            has_long_rest = True
            
        self.assertTrue(has_long_rest, "Driver A did not get a minimum 6-hour rest period.")

if __name__ == '__main__':
    unittest.main()

import unittest
import json
import datetime
from collections import Counter
import math
import pulp
import argparse
import sys

# Assuming the refactored solver is in 'solver.py'
from solver import solve_schedule, process_results

def create_base_test_data(num_hours=24):
    """Creates a base dictionary with default values for a test."""
    now = datetime.datetime.now(datetime.UTC)
    availability = {}
    team_members = [
        {"name": "Driver A", "isDriver": True, "isSpotter": False, "preferredStints": 4, "minimumRestHours": 0, "timezone": 0},
        {"name": "Driver B", "isDriver": True, "isSpotter": True, "preferredStints": 4, "minimumRestHours": 0, "timezone": 0},
        {"name": "Driver C", "isDriver": True, "isSpotter": False, "preferredStints": 4, "minimumRestHours": 0, "timezone": 0},
        {"name": "Spotter D", "isDriver": False, "isSpotter": True, "preferredStints": 4, "minimumRestHours": 0, "timezone": 0},
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
        
        prob, _, total_stints, _, driver_pool, _, drive_vars, _, _ = solve_schedule(data, 60, spotter_mode='none')
        schedule = process_results(prob, total_stints, driver_pool, [], drive_vars, {})
        
        self.assertIsNotNone(schedule, "Solver failed to find a solution.")
        drivers = [s['driver'] for s in schedule]
        self.assertEqual(len(drivers), 9)
        counts = Counter(d for d in drivers if d != 'N/A')
        self.assertEqual(counts['Driver A'], 3)
        self.assertEqual(counts['Driver B'], 3)
        self.assertEqual(counts['Driver C'], 3)

    def test_2_availability_constraint(self):
        """Tests that an unavailable driver is never assigned."""
        print("\n--- Running Test 2: Availability Constraint ---")
        data = create_base_test_data()
        data['durationHours'] = 5.5
        
        start_hour_key_date = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
        availability_key = start_hour_key_date.replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        data['availability']['Driver B'][availability_key] = "Unavailable"
        
        prob, _, total_stints, _, driver_pool, _, drive_vars, _, _ = solve_schedule(data, 60, spotter_mode='none')
        schedule = process_results(prob, total_stints, driver_pool, [], drive_vars, {})
        
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
            availability_key = hour_key_date.replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            data['availability']['Driver A'][availability_key] = "Preferred"
            data['availability']['Driver B'][availability_key] = "Preferred"

        prob, _, total_stints, _, driver_pool, _, drive_vars, _, _ = solve_schedule(data, 60, spotter_mode='none')
        schedule = process_results(prob, total_stints, driver_pool, [], drive_vars, {})
        
        self.assertIsNotNone(schedule, "Solver failed to find a solution.")
        counts = Counter(s['driver'] for s in schedule if s['driver'] != 'N/A')
        min_required_stints = 3
        self.assertGreaterEqual(counts['Driver C'], min_required_stints, "Driver C was not assigned their fair share of stints.")

    def test_4_max_consecutive_stints(self):
        """Tests that the consecutive stint limit is respected."""
        print("\n--- Running Test 4: Max Consecutive Stints ---")
        data = create_base_test_data()
        data['durationHours'] = 10 
        data['teamMembers'][0]['preferredStints'] = 2
        
        prob, _, total_stints, _, driver_pool, _, drive_vars, _, _ = solve_schedule(data, 60, spotter_mode='none')
        schedule = process_results(prob, total_stints, driver_pool, [], drive_vars, {})
        
        self.assertIsNotNone(schedule, "Solver failed to find an optimal solution within the time limit.")
        
        drivers = [s['driver'] for s in schedule]
        max_consecutive_found = 0
        current_consecutive = 0
        current_driver = None
        for driver in drivers:
            if driver == 'Driver A':
                if driver == current_driver:
                    current_consecutive += 1
                else:
                    current_consecutive = 1
                max_consecutive_found = max(max_consecutive_found, current_consecutive)
            else:
                current_consecutive = 0
            current_driver = driver
            
        self.assertLessEqual(max_consecutive_found, 2)

    def test_5_minimum_rest(self):
        """Tests that the minimum rest period is enforced."""
        print("\n--- Running Test 5: Minimum Rest ---")
        data = create_base_test_data(num_hours=24)
        data['durationHours'] = 24
        data['teamMembers'][0]['minimumRestHours'] = 6
        
        prob, data, total_stints, stint_laps, driver_pool, _, drive_vars, _, _ = solve_schedule(data, 60, spotter_mode='none')
        schedule = process_results(prob, total_stints, driver_pool, [], drive_vars, {})
        
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

    def test_6_spotter_integrated_mode(self):
        """Tests the integrated spotter scheduling mode."""
        print("\n--- Running Test 6: Integrated Spotter Mode ---")
        data = create_base_test_data(num_hours=4)
        data['durationHours'] = 3.5 # Approx 6 stints
        
        prob, _, total_stints, _, driver_pool, spotter_pool, drive_vars, spot_vars, _ = solve_schedule(data, 60, spotter_mode='integrated')
        schedule = process_results(prob, total_stints, driver_pool, spotter_pool, drive_vars, spot_vars)

        self.assertIsNotNone(schedule)
        self.assertTrue('spotter' in schedule[0])
        
        for s in schedule:
            if s['driver'] == 'Driver B':
                self.assertNotEqual(s['spotter'], 'Driver B')

    def test_7_spotter_sequential_mode(self):
        """Tests the sequential spotter scheduling mode."""
        print("\n--- Running Test 7: Sequential Spotter Mode ---")
        data = create_base_test_data(num_hours=4)
        data['durationHours'] = 3.5
        
        prob, _, total_stints, _, driver_pool, spotter_pool, drive_vars, spot_vars, _ = solve_schedule(data, 60, spotter_mode='sequential')
        schedule = process_results(prob, total_stints, driver_pool, spotter_pool, drive_vars, spot_vars)

        self.assertIsNotNone(schedule)
        self.assertTrue('spotter' in schedule[0])
        self.assertNotEqual(schedule[0]['spotter'], 'N/A')

    def test_8_allow_no_spotter(self):
        """Tests allowing stints to have no spotter."""
        print("\n--- Running Test 8: Allow No Spotter ---")
        data = create_base_test_data(num_hours=2)
        data['durationHours'] = 1.5 # Approx 3 stints
        
        start_time = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
        second_stint_hour = start_time + datetime.timedelta(hours=1)
        second_stint_key = second_stint_hour.replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        data['availability']['Driver B'][second_stint_key] = "Unavailable"
        data['availability']['Spotter D'][second_stint_key] = "Unavailable"

        prob_fail, _, _, _, _, _, _, _, _ = solve_schedule(data, 60, spotter_mode='integrated', allow_no_spotter=False)
        self.assertNotEqual(prob_fail.status, pulp.LpStatusOptimal, "Solver should fail when a spotter is required but unavailable.")

        prob_ok, _, total_stints, _, driver_pool, spotter_pool, drive_vars, spot_vars, _ = solve_schedule(data, 60, spotter_mode='integrated', allow_no_spotter=True)
        schedule = process_results(prob_ok, total_stints, driver_pool, spotter_pool, drive_vars, spot_vars)
        
        self.assertIsNotNone(schedule)
        spotter_for_stint_2 = schedule[1]['spotter']
        self.assertEqual(spotter_for_stint_2, 'N/A', "Stint 2 should not have a spotter assigned.")

    def test_9_no_drive_and_spot(self):
        """Tests that a driver cannot be a spotter in the same stint."""
        print("\n--- Running Test 9: No Drive and Spot ---")
        data = create_base_test_data(num_hours=4)
        data['durationHours'] = 3.5 # Approx 6 stints
        
        prob, _, total_stints, _, driver_pool, spotter_pool, drive_vars, spot_vars, _ = solve_schedule(data, 60, spotter_mode='integrated')
        schedule = process_results(prob, total_stints, driver_pool, spotter_pool, drive_vars, spot_vars)

        self.assertIsNotNone(schedule)
        for s in schedule:
            if s['driver'] != 'N/A':
                self.assertNotEqual(s['driver'], s['spotter'], f"Stint {s['stint']}: {s['driver']} cannot drive and spot simultaneously.")

    def test_10_first_stint_driver(self):
        """Tests the hard constraint for the first stint driver."""
        print("\n--- Running Test 10: First Stint Driver ---")
        data = create_base_test_data()
        data['durationHours'] = 5.5
        data['firstStintDriver'] = 'Driver C'

        prob, _, total_stints, _, driver_pool, _, drive_vars, _, _ = solve_schedule(data, 60, spotter_mode='none')
        schedule = process_results(prob, total_stints, driver_pool, [], drive_vars, {})

        self.assertIsNotNone(schedule, "Solver failed to find a solution.")
        self.assertEqual(schedule[0]['driver'], 'Driver C', "The first stint was not assigned to the specified driver.")

if __name__ == '__main__':
    # --- Main execution block to allow running single tests ---
    parser = argparse.ArgumentParser(description="Run scheduler tests.")
    parser.add_argument('test_name', nargs='?', default=None, help="The name of a single test to run (e.g., test_5_minimum_rest).")
    parser.add_argument('--list', action='store_true', help="List all available tests and exit.")
    args = parser.parse_args()

    if args.list:
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromTestCase(TestScheduler)
        print("Available tests:")
        for test in suite:
            print(f"  {test.id().split('.')[-1]}")
        sys.exit(0)

    suite = unittest.TestSuite()
    if args.test_name:
        try:
            suite.addTest(TestScheduler(args.test_name))
        except ValueError:
            print(f"Error: Test '{args.test_name}' not found.")
            sys.exit(1)
    else:
        suite = unittest.TestLoader().loadTestsFromTestCase(TestScheduler)

    runner = unittest.TextTestRunner()
    result = runner.run(suite)

    if not result.wasSuccessful():
        sys.exit(1)

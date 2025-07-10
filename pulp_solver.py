import json
import datetime
import logging
import math
import sys
import argparse
import csv
import pulp
import numpy as np

# --- 1. Setup Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def solve_driver_only_schedule(data):
    """
    Formulates and solves the DRIVER-ONLY scheduling problem using PuLP.
    """
    # --- 2. Calculate Race Parameters ---
    lap_time_seconds = data['avgLapTimeInSeconds']
    pit_time_seconds = data['pitTimeInSeconds']
    
    stint_laps = int(data['fuelTankSize'] / data['fuelUsePerLap']) if data['fuelUsePerLap'] > 0 else 0
    stint_with_pit_seconds = (stint_laps * lap_time_seconds) + pit_time_seconds
    race_duration_seconds = data['durationHours'] * 3600
    total_stints = int(np.ceil(race_duration_seconds / stint_with_pit_seconds)) if stint_with_pit_seconds > 0 else 0
    total_laps = total_stints * stint_laps
    
    driver_pool = [m for m in data['teamMembers'] if m['role'] in ['Driver Only', 'Driver and Spotter']]
    num_drivers = len(driver_pool)
    
    equal_share_laps = total_laps / num_drivers if driver_pool else 0
    min_laps_per_driver = math.ceil(0.25 * equal_share_laps)
    min_stints_per_driver = math.ceil(min_laps_per_driver / stint_laps) if stint_laps > 0 else 0

    logging.info(f"Total Stints Calculated: {total_stints}")
    logging.info(f"Fair Share Requirement: Minimum {min_stints_per_driver} stints per driver.")

    # --- 3. Formulate the Optimization Problem using PuLP ---
    logging.info("--- Building Driver-Only Optimization Model with PuLP ---")
    
    prob = pulp.LpProblem("Driver_Scheduling", pulp.LpMinimize)

    # --- Variables ---
    stints = range(total_stints)
    
    drive_vars = pulp.LpVariable.dicts("Drive", ( (m['name'], s) for m in driver_pool for s in stints ), cat='Binary')
    switch_vars = pulp.LpVariable.dicts("Switch", ( (d['name'], s) for d in driver_pool for s in stints if s > 0 ), cat='Binary')
    
    max_drive_stints = pulp.LpVariable("MaxDriveStints", 0, None, 'Integer')
    min_drive_stints = pulp.LpVariable("MinDriveStints", 0, None, 'Integer')

    # --- Pre-calculate preference scores ---
    preference_scores = {}
    for s in stints:
        race_start_utc = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
        stint_start_time = race_start_utc + datetime.timedelta(seconds=s * stint_with_pit_seconds)
        key_time = stint_start_time.replace(minute=0, second=0, microsecond=0)
        availability_key = key_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        for driver in driver_pool:
            if data['availability'][driver['name']].get(availability_key) == 'Preferred':
                preference_scores[(driver['name'], s)] = 1
            else:
                preference_scores[(driver['name'], s)] = 0

    # --- Objective Function: Minimize a weighted cost ---
    prob += (
        (max_drive_stints - min_drive_stints) * 1000 +  # P1: Balance driver workload
        pulp.lpSum(switch_vars[(d['name'], s)] for d in driver_pool for s in stints if s > 0) * 100 - # P2: Maximize rest
        pulp.lpSum(drive_vars[(d['name'], s)] * preference_scores.get((d['name'], s), 0) for d in driver_pool for s in stints) # P3: Reward preferred slots
    ), "Weighted_Cost"

    # --- Constraints ---
    logging.info("--- Adding Constraints to Model ---")

    # NEW CONSTRAINT: First Stint Driver
    if data.get('firstStintDriver'):
        first_driver = data['firstStintDriver']
        # Check if the specified first driver is in the pool of eligible drivers
        if any(d['name'] == first_driver for d in driver_pool):
            logging.info(f"Adding constraint: First stint must be driven by {first_driver}")
            prob += drive_vars[(first_driver, 0)] == 1, "FirstStintDriver"
        else:
            # This case should ideally be caught by the exporter, but good to have a fallback.
            logging.warning(f"FirstStintDriver '{first_driver}' is not an eligible driver (check role). Constraint will be ignored.")


    for s in stints:
        prob += pulp.lpSum(drive_vars.get((m['name'], s), 0) for m in driver_pool) == 1, f"OneDriver_Stint_{s}"

        race_start_utc = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
        stint_start_time = race_start_utc + datetime.timedelta(seconds=s * stint_with_pit_seconds)
        key_time = stint_start_time.replace(minute=0, second=0, microsecond=0)
        availability_key = key_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')

        for driver in driver_pool:
            driver_name = driver['name']
            is_available = data['availability'][driver_name].get(availability_key, 'Unavailable') != 'Unavailable'
            
            if not is_available:
                prob += drive_vars[(driver_name, s)] == 0, f"UnavailableDrive_{driver_name}_{s}"

            # Define switch variables
            if s > 0:
                prob += switch_vars[(driver_name, s)] >= drive_vars[(driver_name, s)] - drive_vars[(driver_name, s-1)]

    for driver in driver_pool:
        driver_name = driver['name']
        total_driver_stints = pulp.lpSum(drive_vars[(driver_name, s)] for s in stints)
        prob += max_drive_stints >= total_driver_stints, f"DefineMaxDrive_{driver_name}"
        prob += min_drive_stints <= total_driver_stints, f"DefineMinDrive_{driver_name}"
        prob += total_driver_stints >= min_stints_per_driver, f"FairShare_{driver_name}"
        
        max_consecutive = driver['preferredStints']
        for s in range(total_stints - max_consecutive):
            prob += pulp.lpSum(drive_vars[(driver_name, s+i)] for i in range(max_consecutive + 1)) <= max_consecutive, f"MaxConsecutive_{driver_name}_{s}"

    # --- 4. Solve the Problem ---
    logging.info("--- Solving... (This may take a moment) ---")
    prob.solve(pulp.PULP_CBC_CMD(msg=1))
    
    return prob, data, total_stints, stint_laps, driver_pool, drive_vars


def process_results(prob, total_stints, driver_pool, drive_vars):
    """Processes the PuLP result and prepares the raw schedule assignments."""
    if prob.status != pulp.LpStatusOptimal:
        logging.error("Could not find an optimal solution.")
        logging.error(f"Solver status: {pulp.LpStatus[prob.status]}")
        return None

    logging.info("Optimal Schedule Found!")
    schedule = []
    for s in range(total_stints):
        assigned_driver = "N/A"
        for driver in driver_pool:
            if pulp.value(drive_vars.get((driver['name'], s))) > 0.5:
                assigned_driver = driver['name']
                break
        schedule.append({"stint": s + 1, "driver": assigned_driver})
    
    return schedule

def main():
    """
    Main function to parse arguments, read data, solve, and save raw results.
    """
    parser = argparse.ArgumentParser(description="Solve for an optimal endurance race schedule.")
    parser.add_argument('input_file', nargs='?', default=None, help="Path to the race_data.json file. If omitted, reads from stdin.")
    parser.add_argument('--output', required=True, help="Path to save the raw schedule results as a JSON file.")
    args = parser.parse_args()

    try:
        if args.input_file:
            logging.info(f"--- Reading Race Data from file: {args.input_file} ---")
            with open(args.input_file, 'r') as f: data = json.load(f)
        else:
            logging.info("--- Reading Race Data from stdin ---")
            data = json.load(sys.stdin)
    except Exception as e:
        logging.error(f"Failed to read or parse input data: {e}")
        return

    prob, data, total_stints, stint_laps, driver_pool, drive_vars = solve_driver_only_schedule(data)
    
    schedule_assignments = process_results(prob, total_stints, driver_pool, drive_vars)

    if schedule_assignments:
        output_data = {
            "raceData": data,
            "schedule": schedule_assignments
        }
        
        logging.info(f"Saving raw schedule results to {args.output}")
        with open(args.output, 'w') as f:
            json.dump(output_data, f, indent=4)
        
        logging.info("Solver finished successfully.")
        
if __name__ == '__main__':
    main()

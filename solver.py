import json
import datetime
import logging
import math
import sys
import argparse
import pulp
import numpy as np
import time

def setup_logging(quiet=False):
    """Configures logging based on the quiet flag."""
    log_level = logging.WARNING if quiet else logging.INFO
    logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

def _add_participant_model(prob, data, participants, stints, var_prefix, stint_laps, stint_with_pit_seconds, allow_no_spotter=False):
    """
    Adds a generic set of variables, constraints, and objectives for a participant type (driver or spotter).
    """
    if not participants:
        return prob, {}

    # --- Variables ---
    work_vars = pulp.LpVariable.dicts(var_prefix, ((p['name'], s) for p in participants for s in stints), cat='Binary')
    switch_vars = pulp.LpVariable.dicts(f"{var_prefix}Switch", ((p['name'], s) for p in participants for s in stints if s > 0), cat='Binary')
    max_work_stints = pulp.LpVariable(f"Max{var_prefix}Stints", 0, None, 'Integer')
    min_work_stints = pulp.LpVariable(f"Min{var_prefix}Stints", 0, None, 'Integer')

    # --- Objective Function ---
    balance_objective = (max_work_stints - min_work_stints) * 1000
    switch_objective = pulp.lpSum(switch_vars.get((p['name'], s), 0) for p in participants for s in stints if s > 0) * 100

    preference_scores = {}
    race_start_utc = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
    for s in stints:
        stint_start_time = race_start_utc + datetime.timedelta(seconds=s * stint_with_pit_seconds)
        key_time = stint_start_time.replace(minute=0, second=0, microsecond=0)
        availability_key = key_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        for p in participants:
            if data['availability'][p['name']].get(availability_key) == 'Preferred':
                preference_scores[(p['name'], s)] = 1
            else:
                preference_scores[(p['name'], s)] = 0
    
    preference_objective = -pulp.lpSum(work_vars[(p['name'], s)] * preference_scores.get((p['name'], s), 0) for p in participants for s in stints)
    prob.objective += balance_objective + switch_objective + preference_objective

    # --- Constraints ---
    for s in stints:
        stint_start_time = race_start_utc + datetime.timedelta(seconds=s * stint_with_pit_seconds)
        key_time = stint_start_time.replace(minute=0, second=0, microsecond=0)
        availability_key = key_time.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        for p in participants:
            is_available = data['availability'][p['name']].get(availability_key, 'Unavailable') != 'Unavailable'
            if not is_available:
                prob += work_vars[(p['name'], s)] == 0, f"Unavailable{var_prefix}_{p['name']}_{s}"
            if s > 0:
                prob += switch_vars[(p['name'], s)] >= work_vars[(p['name'], s)] - work_vars[(p['name'], s - 1)]

    total_laps = len(stints) * stint_laps
    equal_share_laps = total_laps / len(participants) if participants else 0
    min_laps_per_participant = math.ceil(0.25 * equal_share_laps)
    min_stints_per_participant = math.ceil(min_laps_per_participant / stint_laps) if stint_laps > 0 else 0

    for p in participants:
        name = p['name']
        total_participant_stints = pulp.lpSum(work_vars[(name, s)] for s in stints)
        prob += max_work_stints >= total_participant_stints, f"DefineMax{var_prefix}_{name}"
        prob += min_work_stints <= total_participant_stints, f"DefineMin{var_prefix}_{name}"
        
        if var_prefix == 'Drive':
            prob += total_participant_stints >= min_stints_per_participant, f"FairShare{var_prefix}_{name}"

        max_consecutive = p['preferredStints']
        for s in range(len(stints) - max_consecutive):
            prob += pulp.lpSum(work_vars[(name, s + i)] for i in range(max_consecutive + 1)) <= max_consecutive, f"MaxConsecutive{var_prefix}_{name}_{s}"

        min_rest_hours = p.get('minimumRestHours', 0)
        if min_rest_hours > 0 and stint_with_pit_seconds > 0:
            min_rest_stints = math.floor((min_rest_hours * 3600) / stint_with_pit_seconds)
            if min_rest_stints > 0:
                possible_rest_starts = range(len(stints) - min_rest_stints + 1)
                rest_block_achieved = pulp.LpVariable.dicts(f"RestAchieved{var_prefix}_{name}", possible_rest_starts, cat='Binary')
                prob += pulp.lpSum(rest_block_achieved[s] for s in possible_rest_starts) >= 1, f"MustHaveOneRest{var_prefix}_{name}"
                M = min_rest_stints + 1
                for s in possible_rest_starts:
                    prob += pulp.lpSum(work_vars[(name, s + i)] for i in range(min_rest_stints)) <= M * (1 - rest_block_achieved[s]), f"EnforceRest{var_prefix}_{name}_{s}"

    return prob, work_vars

def solve_schedule(data, time_limit, spotter_mode='none', allow_no_spotter=False):
    """Main function to formulate and solve the scheduling problem based on the chosen mode."""
    lap_time_seconds = data['avgLapTimeInSeconds']
    pit_time_seconds = data['pitTimeInSeconds']
    stint_laps = int(data['fuelTankSize'] / data['fuelUsePerLap']) if data['fuelUsePerLap'] > 0 else 0
    stint_with_pit_seconds = (stint_laps * lap_time_seconds) + pit_time_seconds
    race_duration_seconds = data['durationHours'] * 3600
    total_stints = int(np.ceil(race_duration_seconds / stint_with_pit_seconds)) if stint_with_pit_seconds > 0 else 0
    stints = range(total_stints)

    driver_pool = [m for m in data['teamMembers'] if m.get('isDriver')]
    spotter_pool = [m for m in data['teamMembers'] if m.get('isSpotter')] if spotter_mode != 'none' else []
    
    logging.info(f"--- Building a '{spotter_mode}' schedule with {total_stints} stints ---")
    prob = pulp.LpProblem("Race_Scheduling", pulp.LpMinimize)
    
    prob, drive_vars = _add_participant_model(prob, data, driver_pool, stints, "Drive", stint_laps, stint_with_pit_seconds)
    for s in stints:
        prob += pulp.lpSum(drive_vars[(m['name'], s)] for m in driver_pool) == 1, f"OneDriver_Stint_{s}"
    
    if data.get('firstStintDriver'):
        first_driver_name = data['firstStintDriver']
        if any(d['name'] == first_driver_name for d in driver_pool):
            logging.info(f"Adding constraint: First stint must be driven by {first_driver_name}")
            prob += drive_vars[(first_driver_name, 0)] == 1, "FirstStintDriver"
        else:
            logging.warning(f"FirstStintDriver '{first_driver_name}' is not an eligible driver. Constraint ignored.")

    spot_vars = {}
    solve_duration = 0.0

    if spotter_mode == 'sequential' and spotter_pool:
        logging.info("--- Sequential Mode: Step 1: Solving for Drivers ---")
        solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit)
        start_time_1 = time.time()
        prob.solve(solver)
        end_time_1 = time.time()
        solve_duration += (end_time_1 - start_time_1)
        logging.info(f"Driver solve finished in {solve_duration:.2f} seconds.")
        
        if prob.status != pulp.LpStatusOptimal:
            logging.error(f"Could not find an optimal driver schedule. Status: {pulp.LpStatus[prob.status]}")
            return None, None, None, None, None, None, None, None, None

        fixed_driver_schedule = {(d['name'], s): pulp.value(drive_vars[(d['name'], s)]) for d in driver_pool for s in stints}
        
        logging.info("--- Sequential Mode: Step 2: Solving for Spotters ---")
        spotter_prob = pulp.LpProblem("Spotter_Scheduling", pulp.LpMinimize)
        spotter_prob, spot_vars = _add_participant_model(spotter_prob, data, spotter_pool, stints, "Spot", stint_laps, stint_with_pit_seconds, allow_no_spotter)
        
        for s in stints:
            if allow_no_spotter:
                spotter_prob += pulp.lpSum(spot_vars[(m['name'], s)] for m in spotter_pool) <= 1, f"AtMostOneSpotter_Stint_{s}"
            else:
                spotter_prob += pulp.lpSum(spot_vars[(m['name'], s)] for m in spotter_pool) == 1, f"ExactlyOneSpotter_Stint_{s}"
        
        for (name, s), is_driving in fixed_driver_schedule.items():
            member = next((m for m in data['teamMembers'] if m['name'] == name), None)
            if is_driving > 0.5 and member and member.get('isSpotter'):
                spotter_prob += spot_vars[(name, s)] == 0, f"NoSpotWhileDriving_{name}_{s}"
        
        prob = spotter_prob

    elif spotter_mode == 'integrated' and spotter_pool:
        logging.info("--- Integrated Mode: Solving for Drivers and Spotters Simultaneously ---")
        prob, spot_vars = _add_participant_model(prob, data, spotter_pool, stints, "Spot", stint_laps, stint_with_pit_seconds, allow_no_spotter)
        for s in stints:
            if allow_no_spotter:
                prob += pulp.lpSum(spot_vars[(m['name'], s)] for m in spotter_pool) <= 1, f"AtMostOneSpotter_Stint_{s}"
            else:
                prob += pulp.lpSum(spot_vars[(m['name'], s)] for m in spotter_pool) == 1, f"ExactlyOneSpotter_Stint_{s}"
        
        for member in data['teamMembers']:
            if member.get('isDriver') and member.get('isSpotter'):
                for s in stints:
                    prob += drive_vars[(member['name'], s)] + spot_vars[(member['name'], s)] <= 1, f"NoDriveAndSpot_{member['name']}_{s}"

    logging.info(f"--- Solving... (Time limit: {time_limit} seconds) ---")
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit)
    start_time = time.time()
    prob.solve(solver)
    end_time = time.time()
    
    final_solve_duration = end_time - start_time
    solve_duration += final_solve_duration
    logging.info(f"Final solve step finished in {final_solve_duration:.2f} seconds.")
    logging.info(f"Total solver time: {solve_duration:.2f} seconds.")
    
    return prob, data, total_stints, stint_laps, driver_pool, spotter_pool, drive_vars, spot_vars, solve_duration

def process_results(prob, total_stints, driver_pool, spotter_pool, drive_vars, spot_vars):
    """Processes the PuLP result and prepares the schedule assignments."""
    if prob.status != pulp.LpStatusOptimal:
        logging.error(f"Solver did not find an optimal solution. Status: {pulp.LpStatus[prob.status]}")
        return None
    
    schedule = []
    for s in range(total_stints):
        assigned_driver = next((d['name'] for d in driver_pool if pulp.value(drive_vars.get((d['name'], s))) > 0.5), "N/A")
        
        entry = {"stint": s + 1, "driver": assigned_driver}
        if spotter_pool:
            assigned_spotter = next((p['name'] for p in spotter_pool if pulp.value(spot_vars.get((p['name'], s))) > 0.5), "N/A")
            entry["spotter"] = assigned_spotter
        schedule.append(entry)
        
    return schedule

def main():
    """Main function to parse arguments, read data, solve, and output results."""
    parser = argparse.ArgumentParser(description="Solve for an optimal endurance race schedule.")
    parser.add_argument('input_file', nargs='?', default=None, help="Path to the race_data.json file.")
    parser.add_argument('--output', help="Optional. Path to save the schedule as a JSON file.")
    parser.add_argument('--time-limit', type=int, default=30, help="Maximum time in seconds to let the solver run.")
    parser.add_argument('--quiet', action='store_true', help="Suppress INFO logs.")
    parser.add_argument('--spotter-mode', choices=['none', 'integrated', 'sequential'], default='none', help="Method for scheduling spotters.")
    parser.add_argument('--allow-no-spotter', action='store_true', help="Allow stints to have no spotter assigned (only applies to integrated/sequential modes).")
    args = parser.parse_args()

    setup_logging(args.quiet)

    try:
        if args.input_file:
            with open(args.input_file, 'r') as f: data = json.load(f)
        else:
            data = json.load(sys.stdin)
    except Exception as e:
        logging.error(f"Failed to read or parse input data: {e}"); return

    prob, data, total_stints, stint_laps, driver_pool, spotter_pool, drive_vars, spot_vars, solve_duration = solve_schedule(
        data, args.time_limit, args.spotter_mode, args.allow_no_spotter
    )
    
    if prob is None: return

    schedule_assignments = process_results(prob, total_stints, driver_pool, spotter_pool, drive_vars, spot_vars)

    if schedule_assignments:
        if not args.quiet:
            has_spotters = args.spotter_mode != 'none' and spotter_pool
            print("\n--- 🏁 Race Schedule ---")
            for s in schedule_assignments:
                line = f"Stint {s['stint']:<3}: Driver: {s['driver']:<15}"
                if has_spotters:
                    line += f" | Spotter: {s.get('spotter', 'N/A'):<15}"
                print(line)
        
        if args.output:
            output_data = {
                "raceData": data,
                "schedule": schedule_assignments,
                "solveDurationSeconds": solve_duration
            }
            with open(args.output, 'w') as f: json.dump(output_data, f, indent=4)
        logging.info("Solver finished successfully.")

if __name__ == '__main__':
    main()

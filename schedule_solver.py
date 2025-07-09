import json
import datetime
import numpy as np
from scipy.optimize import milp, Bounds
import logging
import math
import sys
import argparse
import csv

# --- 1. Setup Logging ---
# This ensures all messages are timestamped and printed immediately.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def formulate_and_solve(data):
    """
    Formulates the driver scheduling problem and solves it using MILP.
    
    Args:
        data (dict): The dictionary containing all race data.

    Returns:
        A SciPy optimization result object and other calculated parameters.
    """
    # --- 2. Calculate Race Parameters ---
    avg_lap_time_obj = datetime.datetime.strptime(data['avgLapTime'], "%Y-%m-%dT%H:%M:%S.%fZ")
    pit_time_obj = datetime.datetime.strptime(data['pitTime'], "%Y-%m-%dT%H:%M:%S.%fZ")
    
    lap_time_seconds = avg_lap_time_obj.minute * 60 + avg_lap_time_obj.second + avg_lap_time_obj.microsecond / 1e6
    pit_time_seconds = pit_time_obj.minute * 60 + pit_time_obj.second + pit_time_obj.microsecond / 1e6
    
    stint_laps = int(data['fuelTankSize'] / data['fuelUsePerLap'])
    stint_with_pit_seconds = (stint_laps * lap_time_seconds) + pit_time_seconds
    race_duration_seconds = data['durationHours'] * 3600
    total_stints = int(np.ceil(race_duration_seconds / stint_with_pit_seconds))
    total_laps = total_stints * stint_laps
    
    driver_pool = [m for m in data['teamMembers'] if m['role'] in ['Driver Only', 'Driver and Spotter']]
    num_drivers = len(driver_pool)

    # Calculate Fair Share requirement
    equal_share_laps = total_laps / num_drivers if num_drivers > 0 else 0
    min_laps_per_driver = math.ceil(0.25 * equal_share_laps)
    min_stints_per_driver = math.ceil(min_laps_per_driver / stint_laps) if stint_laps > 0 else 0

    logging.info(f"Total Stints Calculated: {total_stints}")
    logging.info(f"Fair Share Requirement: Minimum {min_stints_per_driver} stints per driver.")

    # --- 3. Formulate the Optimization Problem ---
    logging.info("--- Building Driver Optimization Model ---")
    
    num_stints = total_stints
    num_x_vars = num_drivers * num_stints
    num_s_vars = num_stints - 1
    
    c = np.zeros(num_x_vars + num_s_vars + 2)
    balance_weight = 1000
    c[-2] = balance_weight
    c[-1] = -balance_weight
    switch_penalty = 1
    c[num_x_vars:num_x_vars + num_s_vars] = switch_penalty

    integrality = np.ones_like(c)
    lb = np.zeros_like(c)
    ub = np.ones_like(c)
    ub[-2:] = np.inf
    bounds = Bounds(lb=lb, ub=ub)

    constraints = []
    
    # Constraint 1: One driver per stint
    for s in range(num_stints):
        A_row = np.zeros_like(c)
        race_start_utc = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
        stint_start_time = race_start_utc + datetime.timedelta(seconds=s * stint_with_pit_seconds)
        current_hour_utc = str(stint_start_time.hour)
        
        for d in range(num_drivers):
            driver_name = driver_pool[d]['name']
            if data['availability'][driver_name].get(current_hour_utc, 'Unavailable') != 'Unavailable':
                 A_row[d * num_stints + s] = 1
        constraints.append((A_row, 1, 1))

    # Constraint 2: Max consecutive stints
    for d in range(num_drivers):
        max_consecutive = driver_pool[d]['preferredStints']
        for s in range(num_stints - max_consecutive):
            A_row = np.zeros_like(c)
            for i in range(max_consecutive + 1):
                A_row[d * num_stints + s + i] = 1
            constraints.append((A_row, -np.inf, max_consecutive))

    # Constraint 3: Driver switch variables
    for s in range(1, num_stints):
        for d in range(num_drivers):
            A_row = np.zeros_like(c)
            A_row[d * num_stints + s] = 1
            A_row[d * num_stints + s - 1] = -1
            A_row[num_x_vars + s - 1] = -1
            constraints.append((A_row, -np.inf, 0))

    # Constraint 4: Max/min stints variables
    for d in range(num_drivers):
        A_max_row = np.zeros_like(c)
        A_max_row[-2] = 1
        for s in range(num_stints):
            A_max_row[d * num_stints + s] = -1
        constraints.append((A_max_row, 0, np.inf))

        A_min_row = np.zeros_like(c)
        A_min_row[-1] = 1
        for s in range(num_stints):
            A_min_row[d * num_stints + s] = -1
        constraints.append((A_min_row, -np.inf, 0))

    # Constraint 5: Fair Share
    for d in range(num_drivers):
        A_row = np.zeros_like(c)
        for s in range(num_stints):
            A_row[d * num_stints + s] = 1
        constraints.append((A_row, min_stints_per_driver, np.inf))

    A = np.array([row[0] for row in constraints])
    lb_constraints = np.array([row[1] for row in constraints])
    ub_constraints = np.array([row[2] for row in constraints])

    logging.info("--- Solving for Driver Schedule... (This may take a moment) ---")
    res = milp(c=c, integrality=integrality, bounds=bounds, constraints=(A, lb_constraints, ub_constraints))
    return res, data, total_stints, stint_laps, lap_time_seconds, pit_time_seconds, driver_pool

def process_driver_results(res, data, total_stints, stint_laps, driver_pool):
    """
    Processes the driver solver result and prepares the initial schedule.
    """
    if not res.success:
        logging.error("Could not find an optimal driver schedule.")
        logging.error("This may be because the constraints are too restrictive.")
        logging.error(f"Solver message: {res.message}")
        return None

    logging.info("Optimal Driver Schedule Found!")
    schedule_drivers = [""] * total_stints
    solution = res.x
    num_stints = total_stints
    for d in range(len(driver_pool)):
        for s in range(num_stints):
            if solution[d * num_stints + s] > 0.5:
                schedule_drivers[s] = driver_pool[d]['name']
    
    return schedule_drivers

def generate_spotter_schedule(driver_schedule, data, stint_with_pit_seconds):
    """
    Generates a spotter schedule based on the optimized driver schedule.
    """
    logging.info("--- Generating Spotter Schedule ---")
    spotter_pool = [m for m in data['teamMembers'] if m['role'] in ['Driver and Spotter', 'Spotter Only']]
    last_active = {member['name']: -1 for member in data['teamMembers']} # Tracks last stint driving OR spotting

    full_schedule = []

    for s, driver_name in enumerate(driver_schedule):
        # Update last active time for the driver of this stint
        last_active[driver_name] = s
        
        stint_start_time = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ") + datetime.timedelta(seconds=s * stint_with_pit_seconds)
        current_hour_utc = str(stint_start_time.hour)
        
        best_spotter = "N/A"
        highest_score = -1

        for spotter in spotter_pool:
            spotter_name = spotter['name']
            
            # Hard constraints: Cannot be the current driver and must be available
            if spotter_name == driver_name:
                continue
            if data['availability'][spotter_name].get(current_hour_utc, 'Unavailable') == 'Unavailable':
                continue

            # Score based on rest time (time since last active)
            score = s - last_active[spotter_name]
            
            if score > highest_score:
                highest_score = score
                best_spotter = spotter_name
        
        # Update last active time for the chosen spotter
        if best_spotter != "N/A":
            last_active[best_spotter] = s

        full_schedule.append({"stint": s + 1, "driver": driver_name, "spotter": best_spotter})

    return full_schedule

def generate_hourly_summary(schedule, data):
    """Generates a per-member sequential hourly schedule summary."""
    logging.info("--- Generating Hourly Summary ---")
    race_duration_hours = math.ceil(data['durationHours'])
    member_schedules = {
        member['name']: {"schedule": ["Resting"] * race_duration_hours, "tz": member['timezone']} 
        for member in data['teamMembers']
    }
    
    race_start_utc = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")

    for entry in schedule:
        start_time_utc = datetime.datetime.strptime(entry['startTimeUTC'], '%Y-%m-%d %H:%M:%S')
        end_time_utc = datetime.datetime.strptime(entry['endTimeUTC'], '%Y-%m-%d %H:%M:%S')
        
        start_hour_index = math.floor((start_time_utc - race_start_utc).total_seconds() / 3600)
        end_hour_index = math.floor((end_time_utc - race_start_utc).total_seconds() / 3600)

        start_hour_index = max(0, start_hour_index)
        end_hour_index = min(race_duration_hours - 1, end_hour_index)

        driver_name = entry['driver']
        if driver_name in member_schedules:
            for i in range(start_hour_index, end_hour_index + 1):
                if i < race_duration_hours:
                    member_schedules[driver_name]['schedule'][i] = "Driving"

        spotter_name = entry['spotter']
        if spotter_name in member_schedules and spotter_name != "N/A":
            for i in range(start_hour_index, end_hour_index + 1):
                 if i < race_duration_hours and member_schedules[spotter_name]['schedule'][i] != "Driving":
                    member_schedules[spotter_name]['schedule'][i] = "Spotting"
             
    return member_schedules

def generate_member_itineraries(schedule, data, pit_time_seconds):
    """Generates a detailed, localized itinerary for each team member, consolidating consecutive duties and adding rest periods."""
    logging.info("--- Generating Per-Member Itineraries ---")
    
    # Step 1: Build a raw list of all duties for each person
    raw_duties = {member['name']: [] for member in data['teamMembers']}
    for entry in schedule:
        start_time_utc = datetime.datetime.strptime(entry['startTimeUTC'], '%Y-%m-%d %H:%M:%S')
        end_time_utc = datetime.datetime.strptime(entry['endTimeUTC'], '%Y-%m-%d %H:%M:%S')
        
        driver_name = entry['driver']
        if driver_name in raw_duties:
            raw_duties[driver_name].append({"start_utc": start_time_utc, "end_utc": end_time_utc, "activity": "Driving"})
            
        spotter_name = entry['spotter']
        if spotter_name in raw_duties and spotter_name != "N/A":
            raw_duties[spotter_name].append({"start_utc": start_time_utc, "end_utc": end_time_utc, "activity": "Spotting"})

    # Step 2: Consolidate duties and build final itineraries with rest periods
    final_itineraries = {}
    race_start_utc = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
    race_end_utc = race_start_utc + datetime.timedelta(hours=data['durationHours'])
    tz_map = {member['name']: int(member.get('timezone', 0)) for member in data['teamMembers']}

    for name, duties in raw_duties.items():
        if not duties:
            continue

        duties.sort(key=lambda x: x['start_utc'])
        
        consolidated_duties = []
        if duties:
            current_block = duties[0].copy()
            for next_duty in duties[1:]:
                is_contiguous = (next_duty['activity'] == current_block['activity'] and
                                 math.isclose((next_duty['start_utc'] - current_block['end_utc']).total_seconds(), pit_time_seconds))
                
                if is_contiguous:
                    current_block['end_utc'] = next_duty['end_utc']
                else:
                    consolidated_duties.append(current_block)
                    current_block = next_duty.copy()
            consolidated_duties.append(current_block)

        # Step 3: Build final itinerary with local times and rest periods
        final_itineraries[name] = []
        offset_hours = tz_map.get(name, 0)
        last_duty_end_local = race_start_utc + datetime.timedelta(hours=offset_hours)

        for duty in consolidated_duties:
            duty_start_local = duty['start_utc'] + datetime.timedelta(hours=offset_hours)
            duty_end_local = duty['end_utc'] + datetime.timedelta(hours=offset_hours)

            if (duty_start_local - last_duty_end_local).total_seconds() > 60:
                final_itineraries[name].append({
                    "start_local": last_duty_end_local,
                    "end_local": duty_start_local,
                    "activity": "Resting",
                    "duration": duty_start_local - last_duty_end_local
                })
            
            final_itineraries[name].append({
                "start_local": duty_start_local,
                "end_local": duty_end_local,
                "activity": duty['activity'],
                "duration": duty_end_local - duty_start_local
            })
            last_duty_end_local = duty_end_local

        race_end_local = race_end_utc + datetime.timedelta(hours=offset_hours)
        if (race_end_local - last_duty_end_local).total_seconds() > 60:
            final_itineraries[name].append({
                "start_local": last_duty_end_local,
                "end_local": race_end_local,
                "activity": "Resting",
                "duration": race_end_local - last_duty_end_local
            })
        
    return final_itineraries


def format_duration(duration_delta):
    """Formats a timedelta object into a human-readable string."""
    total_seconds = int(duration_delta.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    
    if hours > 0 and minutes > 0:
        return f"for {hours} hour{'s' if hours > 1 else ''} and {minutes} minute{'s' if minutes > 1 else ''}"
    elif hours > 0:
        return f"for {hours} hour{'s' if hours > 1 else ''}"
    elif minutes > 0:
        return f"for {minutes} minute{'s' if minutes > 1 else ''}"
    else:
        return ""

def print_to_stdout(schedule, driver_summary, spotter_summary, hourly_summary, member_itineraries):
    """Prints the full schedule and summaries to the console."""
    if not schedule:
        return
        
    print("\n" + "="*85)
    print(f"{'Stint':<6} | {'Start Time (UTC)':<20} | {'End Time (UTC)':<20} | {'Assigned Driver':<20} | {'Assigned Spotter'}")
    print("-" * 85)
    for entry in schedule:
        print(f"{entry['stint']:<6} | {entry['startTimeUTC']:<20} | {entry['endTimeUTC']:<20} | {entry['driver']:<20} | {entry['spotter']}")
    print("="*85 + "\n")

    if driver_summary:
        print("--- Driver Summary ---")
        print(f"{'Driver':<20} | {'Total Stints':<15} | {'Total Laps'}")
        print("-" * 50)
        for name, stats in driver_summary.items():
            print(f"{name:<20} | {stats['stints']:<15} | {stats['laps']}")
        print("-" * 50 + "\n")
    
    if spotter_summary:
        print("--- Spotter Summary ---")
        print(f"{'Spotter':<20} | {'Total Stints'}")
        print("-" * 35)
        for name, stats in spotter_summary.items():
            print(f"{name:<20} | {stats['stints']}")
        print("-" * 35 + "\n")

    if hourly_summary:
        print("--- Per-Member Hourly Schedule (Sequential Hours from Race Start) ---")
        max_hours = 0
        if hourly_summary:
            max_hours = len(next(iter(hourly_summary.values()))['schedule'])

        header = f"{'Member':<20} | {'TZ':<4} |" + "".join([f"{h+1:^3}" for h in range(max_hours)])
        print(header)
        print("-" * len(header))
        
        status_map = {"Driving": "D", "Spotting": "s", "Resting": "."}

        for name, data in hourly_summary.items():
            display_schedule = "".join([f"{status_map.get(s, '.'):^3}" for s in data['schedule']])
            tz_str = str(data.get('tz', ''))
            try:
                if int(tz_str) >= 0:
                    tz_str = f"+{tz_str}"
            except (ValueError, TypeError):
                tz_str = "N/A"
            print(f"{name:<20} | {tz_str:<4} |{display_schedule}")
        print("-" * len(header) + "\n")

    if member_itineraries:
        print("--- Per-Member Itinerary (Local Time) ---")
        for name, itinerary in member_itineraries.items():
            if not itinerary: continue
            print("\n" + f"--- Schedule for {name} ---")
            for duty in itinerary:
                start_str = duty['start_local'].strftime('%Y-%m-%d %H:%M:%S')
                end_str = duty['end_local'].strftime('%H:%M:%S')
                duration_str = format_duration(duty['duration'])
                print(f"{start_str} - {end_str}  - {duty['activity']} {duration_str}")
        print("\n")


def write_to_file(schedule, filename):
    """Writes the schedule to a CSV or JSON file based on extension."""
    if not schedule:
        return
    
    for entry in schedule:
        if 'laps' not in entry:
            entry['laps'] = 0

    if filename.lower().endswith('.csv'):
        logging.info(f"Writing schedule to CSV: {filename}")
        fieldnames = ["stint", "startTimeUTC", "endTimeUTC", "driver", "spotter", "laps"]
        with open(filename, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(schedule)
    elif filename.lower().endswith('.json'):
        logging.info(f"Writing schedule to JSON: {filename}")
        with open(filename, 'w') as jsonfile:
            json.dump(schedule, jsonfile, indent=4)
    else:
        logging.warning(f"Unknown output file format for {filename}. Please use .csv or .json")


def main():
    """
    Main function to parse arguments, read data, solve, and output results.
    """
    parser = argparse.ArgumentParser(description="Generate an optimal endurance race schedule.")
    parser.add_argument('input_file', nargs='?', default=None, help="Path to the race_data.json file. If omitted, reads from stdin.")
    parser.add_argument('--output-csv', help="Path to save the schedule as a CSV file.")
    parser.add_argument('--output-json', help="Path to save the schedule as a JSON file.")
    args = parser.parse_args()

    try:
        if args.input_file:
            logging.info(f"--- Reading Race Data from file: {args.input_file} ---")
            with open(args.input_file, 'r') as f:
                data = json.load(f)
        else:
            logging.info("--- Reading Race Data from stdin ---")
            data = json.load(sys.stdin)
    except FileNotFoundError:
        logging.error(f"Input file not found: {args.input_file}")
        return
    except json.JSONDecodeError:
        logging.error("Invalid JSON data provided.")
        return

    res, data, total_stints, stint_laps, lap_time_seconds, pit_time_seconds, driver_pool = formulate_and_solve(data)
    
    driver_schedule = process_driver_results(res, data, total_stints, stint_laps, driver_pool)

    if driver_schedule:
        stint_with_pit_seconds = (stint_laps * lap_time_seconds) + pit_time_seconds
        full_schedule_assignments = generate_spotter_schedule(driver_schedule, data, stint_with_pit_seconds)
        
        final_schedule_output = []
        driver_summary = {driver['name']: {'stints': 0, 'laps': 0} for driver in driver_pool}
        spotter_summary = {member['name']: {'stints': 0} for member in data['teamMembers'] if member['role'] in ['Driver and Spotter', 'Spotter Only']}
        
        race_start_utc = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
        current_time = race_start_utc

        for assignment in full_schedule_assignments:
            start_time = current_time
            end_time = current_time + datetime.timedelta(seconds=stint_laps * lap_time_seconds)
            
            final_schedule_output.append({
                "stint": assignment['stint'],
                "startTimeUTC": start_time.strftime('%Y-%m-%d %H:%M:%S'),
                "endTimeUTC": end_time.strftime('%Y-%m-%d %H:%M:%S'),
                "driver": assignment['driver'],
                "spotter": assignment['spotter'],
                "laps": stint_laps
            })
            
            if assignment['driver'] in driver_summary:
                driver_summary[assignment['driver']]['stints'] += 1
                driver_summary[assignment['driver']]['laps'] += stint_laps
            if assignment['spotter'] in spotter_summary:
                spotter_summary[assignment['spotter']]['stints'] += 1
            
            current_time = end_time + datetime.timedelta(seconds=pit_time_seconds)
        
        hourly_summary = generate_hourly_summary(final_schedule_output, data)
        member_itineraries = generate_member_itineraries(final_schedule_output, data, pit_time_seconds)

        print_to_stdout(final_schedule_output, driver_summary, spotter_summary, hourly_summary, member_itineraries)
        if args.output_csv:
            write_to_file(final_schedule_output, args.output_csv)
        if args.output_json:
            write_to_file(final_schedule_output, args.output_json)

if __name__ == '__main__':
    main()

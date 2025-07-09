import json
import datetime
import logging
import math
import sys
import argparse
import csv

# --- 1. Setup Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

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
    """Generates a detailed, localized itinerary for each team member, including only intermediate rest periods."""
    logging.info("--- Generating Per-Member Itineraries ---")
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
            
    final_itineraries = {}
    tz_map = {member['name']: int(member.get('timezone', 0)) for member in data['teamMembers']}

    for name, duties in raw_duties.items():
        if not duties: continue
        duties.sort(key=lambda x: x['start_utc'])
        
        consolidated_duties = []
        if duties:
            current_block = duties[0].copy()
            for next_duty in duties[1:]:
                is_contiguous = (next_duty['activity'] == current_block['activity'] and math.isclose((next_duty['start_utc'] - current_block['end_utc']).total_seconds(), pit_time_seconds))
                if is_contiguous:
                    current_block['end_utc'] = next_duty['end_utc']
                else:
                    consolidated_duties.append(current_block)
                    current_block = next_duty.copy()
            consolidated_duties.append(current_block)

        # FIX: Build the final itinerary, but only add the rest periods that fall *between* duties.
        final_itineraries[name] = []
        offset_hours = tz_map.get(name, 0)
        
        for i, duty in enumerate(consolidated_duties):
            duty_start_local = duty['start_utc'] + datetime.timedelta(hours=offset_hours)
            duty_end_local = duty['end_utc'] + datetime.timedelta(hours=offset_hours)

            # If this isn't the first duty, check for a rest period before it.
            if i > 0:
                prev_duty_end_local = consolidated_duties[i-1]['end_utc'] + datetime.timedelta(hours=offset_hours)
                if (duty_start_local - prev_duty_end_local).total_seconds() > (pit_time_seconds + 1):
                    final_itineraries[name].append({
                        "start_local": prev_duty_end_local,
                        "end_local": duty_start_local,
                        "activity": "Resting",
                        "duration": duty_start_local - prev_duty_end_local
                    })
            
            final_itineraries[name].append({
                "start_local": duty_start_local,
                "end_local": duty_end_local,
                "activity": duty['activity'],
                "duration": duty_end_local - duty_start_local
            })

    return final_itineraries

def format_duration(duration_delta):
    """Formats a timedelta object into a human-readable string."""
    total_seconds = int(duration_delta.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours > 0 and minutes > 0: return f"for {hours} hour{'s' if hours > 1 else ''} and {minutes} minute{'s' if minutes > 1 else ''}"
    elif hours > 0: return f"for {hours} hour{'s' if hours > 1 else ''}"
    elif minutes > 0: return f"for {minutes} minute{'s' if minutes > 1 else ''}"
    else: return ""

def print_to_stdout(schedule, driver_summary, spotter_summary, hourly_summary, member_itineraries, args):
    """Prints the requested schedule and summaries to the console."""
    if not schedule: return
    
    spotters_scheduled = any(entry.get('spotter', 'N/A') != 'N/A' for entry in schedule)

    if spotters_scheduled:
        print("\n" + "="*85)
        print(f"{'Stint':<6} | {'Start Time (UTC)':<20} | {'End Time (UTC)':<20} | {'Assigned Driver':<20} | {'Assigned Spotter'}")
        print("-" * 85)
        for entry in schedule: print(f"{entry['stint']:<6} | {entry['startTimeUTC']:<20} | {entry['endTimeUTC']:<20} | {entry['driver']:<20} | {entry['spotter']}")
        print("="*85 + "\n")
    else:
        print("\n" + "="*62)
        print(f"{'Stint':<6} | {'Start Time (UTC)':<20} | {'End Time (UTC)':<20} | {'Assigned Driver'}")
        print("-" * 62)
        for entry in schedule: print(f"{entry['stint']:<6} | {entry['startTimeUTC']:<20} | {entry['endTimeUTC']:<20} | {entry['driver']}")
        print("="*62 + "\n")

    if driver_summary:
        print("--- Driver Summary ---")
        print(f"{'Driver':<20} | {'Total Stints':<15} | {'Total Laps'}")
        print("-" * 50)
        for name, stats in driver_summary.items(): print(f"{name:<20} | {stats['stints']:<15} | {stats['laps']}")
        print("-" * 50 + "\n")
    
    if spotter_summary and any(stats['stints'] > 0 for stats in spotter_summary.values()):
        print("--- Spotter Summary ---")
        print(f"{'Spotter':<20} | {'Total Stints'}")
        print("-" * 35)
        for name, stats in spotter_summary.items():
            if stats['stints'] > 0:
                print(f"{name:<20} | {stats['stints']}")
        print("-" * 35 + "\n")
    
    if args.with_itineraries:
        if hourly_summary:
            print("--- Per-Member Hourly Schedule (Sequential Hours from Race Start) ---")
            max_hours = len(next(iter(hourly_summary.values()))['schedule']) if hourly_summary else 0
            header = f"{'Member':<20} | {'TZ':<4} |" + "".join([f"{h+1:^3}" for h in range(max_hours)])
            print(header)
            print("-" * len(header))
            status_map = {"Driving": "D", "Spotting": "s", "Resting": "."}
            for name, data in hourly_summary.items():
                display_schedule = "".join([f"{status_map.get(s, '.'):^3}" for s in data['schedule']])
                tz_str = str(data.get('tz', ''))
                try:
                    if int(tz_str) >= 0: tz_str = f"+{tz_str}"
                except (ValueError, TypeError): tz_str = "N/A"
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
    if not schedule: return
    
    for entry in schedule:
        if 'laps' not in entry: entry['laps'] = 0

    spotters_scheduled = any(entry.get('spotter', 'N/A') != 'N/A' for entry in schedule)
    
    if filename.lower().endswith('.csv'):
        logging.info(f"Writing schedule to CSV: {filename}")
        fieldnames = ["stint", "startTimeUTC", "endTimeUTC", "driver", "laps"]
        if spotters_scheduled:
            fieldnames.insert(4, "spotter")
        with open(filename, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            if not spotters_scheduled:
                for entry in schedule:
                    entry.pop('spotter', None)
            writer.writerows(schedule)
    elif filename.lower().endswith('.json'):
        logging.info(f"Writing schedule to JSON: {filename}")
        if not spotters_scheduled:
            for entry in schedule:
                entry.pop('spotter', None)
        with open(filename, 'w') as jsonfile:
            json.dump(schedule, jsonfile, indent=4)
    else:
        logging.warning(f"Unknown output file format for {filename}. Please use .csv or .json")


def main():
    """
    Main function to parse arguments, read data, and generate reports.
    """
    parser = argparse.ArgumentParser(description="Generate reports from a solved race schedule.")
    parser.add_argument('input_file', help="Path to the solved_schedule.json file.")
    parser.add_argument('--output-csv', help="Path to save the schedule as a CSV file.")
    parser.add_argument('--output-json', help="Path to save the schedule as a JSON file.")
    parser.add_argument('--with-itineraries', action='store_true', help="Display per-member itineraries and hourly summaries.")
    args = parser.parse_args()

    try:
        logging.info(f"--- Reading Solved Schedule Data from file: {args.input_file} ---")
        with open(args.input_file, 'r') as f:
            solved_data = json.load(f)
        
        data = solved_data['raceData']
        schedule_assignments = solved_data['schedule']

    except FileNotFoundError:
        logging.error(f"Input file not found: {args.input_file}")
        return
    except (json.JSONDecodeError, KeyError):
        logging.error("Invalid or incomplete JSON data provided. Please provide the output file from the solver.")
        return

    # --- Re-calculate necessary parameters ---
    lap_time_seconds = data['avgLapTimeInSeconds']
    pit_time_seconds = data['pitTimeInSeconds']
    stint_laps = int(data['fuelTankSize'] / data['fuelUsePerLap']) if data['fuelUsePerLap'] > 0 else 0
    driver_pool = [m for m in data['teamMembers'] if m['role'] in ['Driver Only', 'Driver and Spotter']]
    spotter_pool = [m for m in data['teamMembers'] if m['role'] in ['Driver and Spotter', 'Spotter Only']]
    
    # --- Finalize schedule with timestamps and calculate summaries ---
    final_schedule_output = []
    driver_summary = {driver['name']: {'stints': 0, 'laps': 0} for driver in driver_pool}
    spotter_summary = {member['name']: {'stints': 0} for member in spotter_pool}
    
    race_start_utc = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
    current_time = race_start_utc

    for assignment in schedule_assignments:
        start_time = current_time
        end_time = current_time + datetime.timedelta(seconds=stint_laps * lap_time_seconds)
        
        final_schedule_output.append({
            "stint": assignment['stint'],
            "startTimeUTC": start_time.strftime('%Y-%m-%d %H:%M:%S'),
            "endTimeUTC": end_time.strftime('%Y-%m-%d %H:%M:%S'),
            "driver": assignment['driver'],
            "spotter": assignment.get('spotter', 'N/A'),
            "laps": stint_laps
        })
        
        if assignment['driver'] in driver_summary:
            driver_summary[assignment['driver']]['stints'] += 1
            driver_summary[assignment['driver']]['laps'] += stint_laps
        if assignment.get('spotter') in spotter_summary:
            spotter_summary[assignment['spotter']]['stints'] += 1
        
        current_time = end_time + datetime.timedelta(seconds=pit_time_seconds)
    
    hourly_summary = None
    member_itineraries = None
    if args.with_itineraries:
        hourly_summary = generate_hourly_summary(final_schedule_output, data)
        member_itineraries = generate_member_itineraries(final_schedule_output, data, pit_time_seconds)

    print_to_stdout(final_schedule_output, driver_summary, spotter_summary, hourly_summary, member_itineraries, args)
    if args.output_csv:
        write_to_file(final_schedule_output, args.output_csv)
    if args.output_json:
        write_to_file(final_schedule_output, args.output_json)

if __name__ == '__main__':
    main()

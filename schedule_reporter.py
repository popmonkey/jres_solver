import json
import datetime
import logging
import math
import sys
import argparse
import csv
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# --- 1. Setup Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def format_duration(duration_delta):
    """Formats a timedelta object into a human-readable string."""
    total_seconds = int(duration_delta.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours > 0 and minutes > 0: return f"for {hours} hour{'s' if hours > 1 else ''} and {minutes} minute{'s' if minutes > 1 else ''}"
    elif hours > 0: return f"for {hours} hour{'s' if hours > 1 else ''}"
    elif minutes > 0: return f"for {minutes} minute{'s' if minutes > 1 else ''}"
    else: return ""

def generate_member_itineraries(schedule, data, pit_time_seconds):
    """Generates a detailed, localized itinerary for each team member, consolidating consecutive duties and adding rest periods."""
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
    race_start_utc = datetime.datetime.strptime(data['raceStartUTC'], "%Y-%m-%dT%H:%M:%S.%fZ")
    race_end_utc = race_start_utc + datetime.timedelta(hours=data['durationHours'])
    tz_map = {member['name']: int(member.get('timezone', 0)) for member in data['teamMembers']}

    for name, duties in raw_duties.items():
        if not duties:
            final_itineraries[name] = []
            continue
        
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

        final_itineraries[name] = []
        offset_hours = tz_map.get(name, 0)
        last_duty_end_local = race_start_utc + datetime.timedelta(hours=offset_hours)

        for duty in consolidated_duties:
            duty_start_local = duty['start_utc'] + datetime.timedelta(hours=offset_hours)
            duty_end_local = duty['end_utc'] + datetime.timedelta(hours=offset_hours)

            if (duty_start_local - last_duty_end_local).total_seconds() > 1:
                final_itineraries[name].append({
                    "start_local": last_duty_end_local,
                    "end_local": duty_start_local,
                    "activity": "Resting",
                })
            
            final_itineraries[name].append({
                "start_local": duty_start_local,
                "end_local": duty_end_local,
                "activity": duty['activity'],
            })
            last_duty_end_local = duty_end_local

        race_end_local = race_end_utc + datetime.timedelta(hours=offset_hours)
        if (race_end_local - last_duty_end_local).total_seconds() > 1:
            final_itineraries[name].append({
                "start_local": last_duty_end_local,
                "end_local": race_end_local,
                "activity": "Resting",
            })
        
    return final_itineraries


def write_to_xlsx(schedule, driver_summary, spotter_summary, member_itineraries, filename):
    """Writes all schedule data and summaries to a multi-sheet XLSX file with calendar view."""
    logging.info(f"Writing full schedule report to XLSX: {filename}")
    
    wb = Workbook()
    
    # --- Sheet 1: Summaries ---
    ws_summary = wb.active
    ws_summary.title = "Summaries"
    bold_font = Font(bold=True)
    
    ws_summary.cell(row=1, column=1, value="Driver Summary").font = bold_font
    ws_summary.append(["Driver", "Total Stints", "Total Laps"])
    for cell in ws_summary["2:2"]: cell.font = bold_font
    for name, stats in driver_summary.items():
        ws_summary.append([name, stats['stints'], stats['laps']])
    
    next_row = ws_summary.max_row + 2
    ws_summary.cell(row=next_row, column=1, value="Spotter Summary").font = bold_font
    ws_summary.append(["Spotter", "Total Stints"])
    for cell in ws_summary[f"{next_row+1}:{next_row+1}"]: cell.font = bold_font
    for name, stats in spotter_summary.items():
        if stats['stints'] > 0:
            ws_summary.append([name, stats['stints']])

    # --- Sheet 2: Master Schedule ---
    ws_master = wb.create_sheet("Master Schedule (UTC)")
    ws_master.append(["Stint", "Start Time (UTC)", "End Time (UTC)", "Assigned Driver", "Assigned Spotter", "Laps"])
    for cell in ws_master["1:1"]: cell.font = bold_font
    for entry in schedule:
        ws_master.append([entry['stint'], entry['startTimeUTC'], entry['endTimeUTC'], entry['driver'], entry['spotter'], entry['laps']])

    # --- Per-Member Itinerary Calendar Sheets ---
    fills = {
        "Driving": PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
        "Spotting": PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"),
        "Resting": PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
    }
    thin_border = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))
    center_align = Alignment(horizontal='center', vertical='center', wrap_text=True)

    for name, itinerary in member_itineraries.items():
        if not itinerary: continue
        
        ws_member = wb.create_sheet(name[:30]) # Sheet name limit is 31 chars
        
        # Create a pandas DataFrame for the calendar grid
        start_date = itinerary[0]['start_local'].date()
        end_date = itinerary[-1]['end_local'].date()
        date_range = pd.date_range(start_date, end_date)
        
        columns = [date.strftime('%Y-%m-%d') for date in date_range]
        index = [f"{h:02d}:{m:02d}" for h in range(24) for m in range(0, 60, 15)]
        df = pd.DataFrame(index=index, columns=columns)

        # Populate the DataFrame with activities
        for duty in itinerary:
            start = duty['start_local']
            end = duty['end_local']
            
            current_time = start
            while current_time < end:
                date_str = current_time.strftime('%Y-%m-%d')
                time_str = current_time.strftime('%H:%M')
                
                # Find the correct 15-minute block
                minute_block = (current_time.minute // 15) * 15
                time_index = f"{current_time.hour:02d}:{minute_block:02d}"

                if date_str in df.columns and time_index in df.index:
                    df.loc[time_index, date_str] = duty['activity']
                
                current_time += datetime.timedelta(minutes=15)
        
        # Write DataFrame to Excel sheet
        ws_member.cell(row=1, column=1, value=f"Schedule for {name}").font = bold_font
        ws_member.cell(row=2, column=1, value="Time (Local)").font = bold_font
        for c_idx, col_name in enumerate(df.columns, 2):
            ws_member.cell(row=2, column=c_idx, value=col_name).font = bold_font
        
        for r_idx, row_name in enumerate(df.index, 3):
            ws_member.cell(row=r_idx, column=1, value=row_name)

        for r_idx, row in enumerate(df.values, 3):
            for c_idx, value in enumerate(row, 2):
                cell = ws_member.cell(row=r_idx, column=c_idx, value=value)
                cell.fill = fills.get(value, fills["Resting"])
                cell.border = thin_border
                cell.alignment = center_align

    # Auto-size columns for all sheets
    for sheet in wb.worksheets:
        for col in sheet.columns:
            sheet.column_dimensions[col[0].column_letter].width = 15

    wb.save(filename)


def main():
    """
    Main function to parse arguments, read data, and generate reports.
    """
    parser = argparse.ArgumentParser(description="Generate reports from a solved race schedule.")
    parser.add_argument('input_file', help="Path to the solved_schedule.json file.")
    parser.add_argument('--output-xlsx', help="Path to save the full report as an XLSX file.")
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
    
    member_itineraries = generate_member_itineraries(final_schedule_output, data, pit_time_seconds)

    if args.output_xlsx:
        write_to_xlsx(final_schedule_output, driver_summary, spotter_summary, member_itineraries, args.output_xlsx)
    else:
        logging.warning("No XLSX output file specified. Run with --output-xlsx <filename> to generate a report.")
        # Default to console output if no file is specified
        # print_to_stdout(final_schedule_output, driver_summary, spotter_summary, None, member_itineraries, args)


if __name__ == '__main__':
    main()

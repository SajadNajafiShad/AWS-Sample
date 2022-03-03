######################################################################
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. #
# SPDX-License-Identifier: MIT-0                                     #
######################################################################

import time
import datetime
import json
import argparse
import joblib
import numpy as np
import pandas as pd
from scipy import interpolate
import os
from tqdm import tqdm
import random
import copy
import matplotlib.pyplot as plt
from pathlib import Path
import seaborn as sns
from vehicle_data import vehicle_dict_ref, battery_dict
from sklearn.preprocessing import MinMaxScaler
from sklearn.model_selection import train_test_split

def calc_degradation(cum_trip_dist, cum_trip_hours, cum_load):
    """
    Enforce a degradation in the sample dataset
    :param cum_trip_dist: Cumulative Distance Travelled
    :param cum_trip_hours: Cumulative Trip Hours Logged
    :param cum_load: Cumulative Load carried by the vehicle
    :return:
    """
    nTripFactor = 10
    distFactor = 200  # Used in distance distribution
    tripTimeFactor = 4.0  # Used in trip time distribution
    loadFactor = 10000
    ret_deg_factor = \
        (np.power((1 + 0.35), cum_trip_dist / (nTripFactor * distFactor)) - 1.) + \
        (np.power((1 + 0.35), cum_trip_hours / (nTripFactor * tripTimeFactor)) - 1.) + \
        (np.power((1 + 0.35), cum_load / (nTripFactor * loadFactor)) - 1.)

    return ret_deg_factor * 0.01


def gen_single_route(trip_len_miles, trip_duration_hrs, trip_load_lb, vehicle_dict, delta_len_miles=0.1):
    """

    Calculate the Kwh usage along the trip

    :param trip_len_miles: Length of the Trip in miles
    :param trip_duration_hrs: Duration of Trip in Hours
    :param trip_load_lb: Loaded vehicle weight
    :param vehicle_dict: Vehicle specific inputs
    :param delta_len_miles: Export simulation data for this length discretization
    :return:
    """
    trip_avg_speed = trip_len_miles / trip_duration_hrs

    avg_kwh_mile = 50. / 80.  # 80 miles in 50 KWh
    drag_force_fraction = 0.3  # 30% of propulsive brake power
    rolling_resistance_fraction = 0.1  # 10% of propulsive brake power
    load_fraction = 0.2  # 20% of the brake power

    kwh_mile = avg_kwh_mile * (1 + \
                               drag_force_fraction * (1.0 - vehicle_dict['scaler']['cd']) + \
                               drag_force_fraction * (trip_avg_speed / 60.0) ** 2 + \
                               rolling_resistance_fraction * (1 - vehicle_dict['scaler']['rolling_resistance']) + \
                               load_fraction * (trip_load_lb / vehicle_dict['unladen_wt_lbs'])
                               )

    # Sample at delta_len Miles
    trip_dist_1d = np.arange(0, 0.9 * trip_len_miles, delta_len_miles)

    # Get higher density points (10X) during last 10% of trip to model the steep discharge
    trip_dist_1d = np.append(trip_dist_1d, np.arange(0.9 * trip_len_miles, trip_len_miles, 0.1 * delta_len_miles))

    kwh_1d = kwh_mile * trip_dist_1d
    time_1d = trip_dist_1d / trip_avg_speed

    ret_pd = pd.DataFrame()
    ret_pd['trip_time'] = time_1d
    ret_pd['trip_kwh'] = kwh_1d
    ret_pd['trip_dist'] = trip_dist_1d
    return ret_pd


def add_v_battery(trip_pd, battery_dict, degradation=0.):
    """

    For a given route and kwh consumption
    Convert the requirement to a per-cell basis and generate the voltage characteristic

    :param trip_pd: Pandas dataframe to be augmented with Cell Characteristics
    :param battery_dict: Battery Discharge curve specification
    :param degradation: Enforce a degradation to simulate depleted full battery capacity
    :return: Populated DataFrame
    """
    trip_kwh = trip_pd['trip_kwh'].values
    trip_battery_peak_capacity = battery_dict['num_cells'] * battery_dict['capacity']
    trip_battery_degraded_capacity = (1 - degradation) * trip_battery_peak_capacity
    trip_soc = (trip_battery_degraded_capacity - trip_kwh * 1000) / trip_battery_peak_capacity

    trip_kwh_cell = trip_kwh * 1000. / (battery_dict['num_cells'])
    f = interpolate.interp1d(np.array(battery_dict['D_Wh']) * (1 - degradation),
                             np.array(battery_dict['v']) * (1 - degradation),
                             kind='slinear',
                             bounds_error=False,
                             fill_value=np.nan)
    v = f(trip_kwh_cell)
    trip_pd['trip_v'] = v
    trip_pd['soc'] = trip_soc
    return trip_pd

def generate_data(analyticSettings):

    n_vehicles = int(analyticSettings['n_vehicles'])
    n_routes = int(analyticSettings['n_routes'])
    rel_dest_path = analyticSettings['rel_dest_path']

    data_path_full = '%s'%rel_dest_path

    work_dir = data_path_full #'./%s'%(run_ID)
    plots_dir = '%s/plots'%(work_dir)
    genPlot = True
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(plots_dir, exist_ok=True)

    route_pd_list = []

    out_trip_pd_fname = work_dir + '/trip_pd.csv'
    fid = open(out_trip_pd_fname, 'w')
    fid.write('vin,route,degradation,trip_dist,trip_duration,trip_load,cum_dist,cum_duration,cum_load\n')

    cum_trip_dist = np.zeros(n_vehicles)
    cum_trip_duration = np.zeros(n_vehicles)
    cum_trip_load = np.zeros(n_vehicles)
    for i_vehicle in tqdm(range(n_vehicles)):
        datetime_base = datetime.datetime(2020, 1, 1)
        for i_route in (range(n_routes)):
            # Normal distribution with mean of 200, std dev of 20 miles
            cur_trip_dist = np.random.normal(loc=200, scale=20)
            # Enforce Clipping Bounds (with Trip > 50 miles)
            while cur_trip_dist <= 50:
                cur_trip_dist = np.random.normal(loc=200, scale=20)

            # Normal Distribution of 5 and std dev of 0.5
            cur_trip_duration = np.random.normal(loc=5, scale=0.5)
            # Enforce clipping bounds trim duration to be > 1.0
            while cur_trip_duration <= 1.0:
                cur_trip_duration = np.random.normal(loc=5, scale=0.5)
            # Trip Load normally distributed around mean 10000 and std dev of 1000
            cur_trip_load = np.random.normal(loc=10000, scale=1000)
            # Clip non physical loads
            while cur_trip_load <= 0:
                cur_trip_load = np.random.normal(loc=10000, scale=1000)

            add_pd = gen_single_route(cur_trip_dist, cur_trip_duration, cur_trip_load, vehicle_dict_ref,
                                      delta_len_miles=1.0)

            # Implement the logic of degradation following a trend

            cur_trip_degradation = calc_degradation(cum_trip_dist[i_vehicle],
                                                    cum_trip_duration[i_vehicle],
                                                    cum_trip_load[i_vehicle])

            degradation_set = cur_trip_degradation

            add_pd = add_v_battery(add_pd, battery_dict, degradation=degradation_set)
            add_pd['route'] = i_route
            add_pd['vin'] = 'V' + str(i_vehicle)
            add_pd['datetime'] = datetime_base + (24 * i_route + add_pd['trip_time']) * datetime.timedelta(hours=1)
            add_pd['cum_trip_dist'] = cum_trip_dist[i_vehicle] + add_pd['trip_dist'].values

            # Taking a max because the trip can be pruned if battery cannot meet request!!
            cum_trip_dist[i_vehicle] += np.max(add_pd['trip_dist'].values)
            cum_trip_duration[i_vehicle] += np.max(add_pd['trip_time'].values)
            cum_trip_load[i_vehicle] += cur_trip_load

            route_pd_list.append(add_pd)
            fid.write('V%d,%d,%8.7e,%f,%f,%f,%f,%f,%f\n' % (
            i_vehicle, i_route, degradation_set, cur_trip_dist, cur_trip_duration, cur_trip_load,
            cum_trip_dist[i_vehicle], cum_trip_duration[i_vehicle], cum_trip_load[i_vehicle]))

    fid.close()
    overall_pd = pd.concat(route_pd_list)
    overall_pd = overall_pd.dropna()
    print('Shape of Overall Pandas DataFrame: %s'%(str(overall_pd.shape)))
    out_overall_pd_fname = work_dir + '/overall_pd.csv'

    overall_pd.to_csv(out_overall_pd_fname)

    grouped_pd = overall_pd.groupby(by=['vin'])

    if genPlot:
        fig, ax = plt.subplots(4, 1, figsize=(10, 4), sharex=True)
        for key, grp in grouped_pd:
            x_axis_column = 'datetime'
            ax[0].plot(grp[x_axis_column], grp['soc'], label=key, linestyle='--', marker='.')
            ax[1].plot(grp[x_axis_column], grp['trip_v'], label=key, linestyle='--', marker='.')
            ax[2].plot(grp[x_axis_column], grp['trip_dist'], label=key, linestyle='--', marker='.')
            ax[3].plot(grp[x_axis_column], grp['cum_trip_dist'], label=key, linestyle='--', marker='.')
        ax[0].set_ylabel('Trip SOC')
        ax[1].set_ylabel('Trip V')
        ax[-1].set_ylabel('Trip Distance')
        ax[-1].set_xlabel('Time (Hrs)')
        ax[0].grid()
        ax[1].grid()
        ax[-1].grid()
        ax[0].legend()
        fig.savefig(plots_dir + '/SOC_V_Dist.png')
        fig2 = plt.figure(2)
        for key, grp in grouped_pd:
            plt.plot(grp['cum_trip_dist'], grp['trip_v'], linestyle='--', marker='.')
        plt.grid()
        plt.xlabel('Cumulative Trip Distance (mi)')
        plt.ylabel('Trip Voltage (V)')
        fig2.savefig(plots_dir + '/cumDist.png')

def preprocess_data(analyticSettings):
    # Since it is a single task.. for the preprocess src is same as dest
    rel_src_path = analyticSettings['rel_dest_path']
    rel_dest_path = analyticSettings['rel_dest_path']

    src_path_full = '%s'%rel_src_path
    dest_path_full = '%s'%rel_dest_path

    overall_pd_fname = '%s/overall_pd.csv' % (src_path_full)
    trip_pd_fname = '%s/trip_pd.csv' % (src_path_full)

    os.makedirs(dest_path_full, exist_ok=True)


    overall_pd = pd.read_csv(overall_pd_fname)
    trip_pd = pd.read_csv(trip_pd_fname)

    rename_dict = {
        'VehicleID': 'vin',
        'RouteID': 'route',
        'Trip_Load': 'trip_load'
    }

    trip_pd.rename(columns=rename_dict, inplace=True)

    # Bring in only the load column
    merged_pd = pd.merge(overall_pd, trip_pd[['vin', 'route', 'trip_load']], how='left', on=['vin', 'route'])
    merged_pd['trip_vel'] = merged_pd['trip_dist'] / merged_pd['trip_time']
    # Fill the Nan In teh first row of velocity
    merged_pd = merged_pd.bfill()

    model_inputs = ['trip_vel', 'trip_dist', 'trip_load']
    model_outputs = ['trip_v']
    model_inputs_outputs = model_inputs + model_outputs

    unique_vin_list = list(merged_pd['vin'].unique())
    unique_route_list = list(merged_pd['route'].unique())

    # Normalize the entire dataset in one go
    all_raw_x = merged_pd[model_inputs].values
    all_raw_y = merged_pd[model_outputs].values

    all_scaler_x  = MinMaxScaler().fit(all_raw_x)
    all_scaler_y  = MinMaxScaler().fit(all_raw_y)

    # Dump the scaler to the pickle file
    scaler_file = dest_path_full + '/' + 'scaler.save'
    joblib.dump([all_scaler_x, all_scaler_y], scaler_file)


    # Loop through the data and export the data along with its metadata
    ret_list = []
    merged_pd_group = merged_pd.groupby(by=['vin', 'route'])
    for cur_vin in tqdm(unique_vin_list):
        for cur_route in unique_route_list:
            # Extract the raw values
            add_pd = merged_pd_group.get_group((cur_vin, cur_route))[model_inputs_outputs]

            # Scale them
            loc_x = all_scaler_x.transform(add_pd[model_inputs].values)
            loc_y =  all_scaler_y.transform(add_pd[model_outputs].values)

            # Dump them into respective directories
            inp_out_filepath_rel = '%s_%d/input_output.json'%(cur_vin, cur_route)
            inp_out_filepath_full = '%s/%s'%(dest_path_full, inp_out_filepath_rel)

            p = Path(inp_out_filepath_full)
            p.parent.mkdir(exist_ok=True)

            input_val_dict = {}
            for idx_x, cur_x in enumerate(model_inputs):
                input_val_dict[cur_x] = loc_x[:, idx_x].tolist()

            output_val_dict = {}
            for idx_y, cur_y in enumerate(model_outputs):
                output_val_dict[cur_y] = loc_y[:, idx_y].tolist()

            export_meta_dict = {
                'vehicle_id': cur_vin,
                'route_id': str(cur_route),
                'dataLocation': inp_out_filepath_rel
            }
            export_dict = {
                'inputs': input_val_dict,
                'outputs': {
                    'actual': output_val_dict,
                    'prediction': {}
                }
            }

            ret_list.append(export_meta_dict)
            with open(inp_out_filepath_full, 'w') as fp:
                json.dump(export_dict, fp)

    # Export the data for building the model also into the master folder
    model_data_pd_list = []
    for cur_vin in tqdm(unique_vin_list):
        cur_route = 0
        # Extract the raw values
        add_pd = merged_pd_group.get_group((cur_vin, cur_route))[model_inputs_outputs]
        model_data_pd_list.append(add_pd)

    model_data_pd = pd.concat(model_data_pd_list)
    # Scale them
    loc_x = all_scaler_x.transform(model_data_pd[model_inputs].values)
    loc_y = all_scaler_y.transform(model_data_pd[model_outputs].values)

    # Dump them into model_data directory
    inp_out_filepath_rel = 'train_data/input_output.json'
    inp_out_filepath_full = '%s/%s' % (dest_path_full, inp_out_filepath_rel)
    p = Path(inp_out_filepath_full)
    p.parent.mkdir(exist_ok=True)
    input_val_dict = {}
    for idx_x, cur_x in enumerate(model_inputs):
        input_val_dict[cur_x] = loc_x[:, idx_x].tolist()

    output_val_dict = {}
    for idx_y, cur_y in enumerate(model_outputs):
        output_val_dict[cur_y] = loc_y[:, idx_y].tolist()

    export_meta_dict = {
        'vehicle_id': "All",
        'route_id': '0',
        'dataLocation': inp_out_filepath_rel
    }
    export_dict = {
        'inputs': input_val_dict,
        'outputs': {
            'actual': output_val_dict,
            'prediction': {}
        }
    }

    ret_list.append(export_meta_dict)
    with open(inp_out_filepath_full, 'w') as fp:
        json.dump(export_dict, fp)

    # Dump the task_details json into the destination folder
    ret_list_json = {'metadata': ret_list}
    task_details_filepath_full = '%s/metadata.json'%(dest_path_full)
    with open(task_details_filepath_full, 'w') as fp:
        json.dump(ret_list_json, fp)
        print('Exported: %s'%(task_details_filepath_full))

def export_status(task_json_path_full):
    # Export the status file
    # Export a model_details with successful state
    timestr = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    status_path_full = '%s_status.json'%(os.path.splitext(task_json_path_full)[0])

    with open(status_path_full, 'w') as fp:
        status_dict = {
            'timestamp': timestr,
            'status': 0
        }
        json.dump(status_dict, fp)
        print('Task status: %s'%(status_path_full))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', help='Relative path to the json file describing this data generation task')
    args = parser.parse_args()

    task_json_path_full = args.config
    with open(task_json_path_full, 'r') as fp:
        task_dict = json.load(fp)
        generate_data(analyticSettings=task_dict['analyticSettings'])

        preprocess_data(analyticSettings=task_dict['analyticSettings'])

        export_status(task_json_path_full=task_json_path_full)

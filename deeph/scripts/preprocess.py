import os
import subprocess as sp
import time
from copy import copy

import numpy as np
import argparse
from pathos.multiprocessing import ProcessingPool as Pool

from deeph import get_preprocess_config, get_rc, get_rh, abacus_parse, siesta_parse


def collect_magmom_from_openmx(input_dir, output_dir, num_atom, mag_element):
    magmom_data = np.zeros((num_atom, 4))

    cmd = f'grep --text -A {num_atom + 3} "Total spin moment" {os.path.join(input_dir, "openmx.scfout")}'
    magmom_str = os.popen(cmd).read().splitlines()
    # print("Total local magnetic moment:", magmom_str[0].split()[4])

    for index in range(num_atom):
        line = magmom_str[3 + index].split()
        assert line[0] == str(index + 1)
        element_str = line[1]
        magmom_r = line[5]
        magmom_theta = line[6]
        magmom_phi = line[7]
        magmom_data[index] = int(element_str in mag_element), magmom_r, magmom_theta, magmom_phi

    np.savetxt(os.path.join(output_dir, "magmom.txt"), magmom_data)

def collect_magmom_from_abacus(input_dir, output_dir, abacus_suffix, num_atom, mag_element): #to use this feature, be sure to turn out_chg and out_mul in abacus INPUT file, if not, will use mag setting in STRU file, and this may loss accuracy or incorrect
    magmom_data = np.zeros((num_atom, 4))

    # using running_scf.log file with INPUT file out_chg and out_mul == 1
    cmd = f"grep 'Total Magnetism' {os.path.join(input_dir, 'OUT.' + abacus_suffix, 'running_scf.log')}"
    datas = os.popen(cmd).read().strip().splitlines()
    if datas:
        for index, data in enumerate(datas):
            element_str = data.split()[4]
            x, y, z = map(float, data.split('(')[-1].split(')')[0].split(','))
            vector = np.array([x, y, z])
            r = np.linalg.norm(vector)
            theta = np.degrees(np.arctan2(vector[1], vector[0]))
            phi = np.degrees(np.arccos(vector[2] / r))
            magmom_data[index] = int(element_str in mag_element), r, theta, phi
    else: # using STRU file magmom setting, THIS MAY CAUSE WRONG OUTPUT!
        index_atom = 0
        with open(os.path.join(input_dir, "STRU"), 'r') as file:
            lines = file.readlines()
            for k in range(len(lines)): # k = line index
                if lines[k].strip() == 'ATOMIC_POSITIONS':
                    kk = k + 2 # kk = current line index
                    while kk < len(lines):
                        if lines[kk] == "\n": # for if empty line between two elements, as ABACUS accepts
                            continue
                        element_str = lines[kk].strip()
                        element_amount = int(lines[kk + 2].strip())
                        for j in range(element_amount):
                            line = lines[kk + 3 + j].strip().split()
                            if len(line) < 11: # check if magmom is included
                                raise ValueError('this line do not contain magmom: {} in this file: {}'.format(line, input_dir))
                            if line[7] != "angle1" and line[8] != "angle1": # check if magmom is in angle mode
                                raise ValueError('mag in STRU should be mag * angle1 * angle2 *')
                            if line[6] == "mag": # for if 'm' is included
                                index_str = 7
                            else:
                                index_str = 8
                            magmom_data[index_atom] = int(element_str in mag_element), line[index_str], line[index_str + 2], line[index_str + 4]
                            index_atom += 1
                        kk += 3 + element_amount

    np.savetxt(os.path.join(output_dir, "magmom.txt"), magmom_data)

def main():
    parser = argparse.ArgumentParser(description='Deep Hamiltonian')
    parser.add_argument('--config', default=[], nargs='+', type=str, metavar='N')
    args = parser.parse_args()

    print(f'User config name: {args.config}')
    config = get_preprocess_config(args.config)

    raw_dir = os.path.abspath(config.get('basic', 'raw_dir'))
    processed_dir = os.path.abspath(config.get('basic', 'processed_dir'))
    abacus_suffix = str(config.get('basic', 'abacus_suffix', fallback='ABACUS'))
    target = config.get('basic', 'target')
    interface = config.get('basic', 'interface')
    local_coordinate = config.getboolean('basic', 'local_coordinate')
    multiprocessing = config.getint('basic', 'multiprocessing')
    get_S = config.getboolean('basic', 'get_S')

    julia_interpreter = config.get('interpreter', 'julia_interpreter')

    def make_cmd(input_dir, output_dir, target, interface, get_S):
        if interface == 'openmx':
            if target == 'hamiltonian':
                cmd = f"{julia_interpreter} " \
                      f"{os.path.join(os.path.dirname(os.path.dirname(__file__)), 'preprocess', 'openmx_get_data.jl')} " \
                      f"--input_dir {input_dir} --output_dir {output_dir} --save_overlap {str(get_S).lower()}"
            elif target == 'density_matrix':
                cmd = f"{julia_interpreter} " \
                      f"{os.path.join(os.path.dirname(os.path.dirname(__file__)), 'preprocess', 'openmx_get_data.jl')} " \
                      f"--input_dir {input_dir} --output_dir {output_dir} --save_overlap {str(get_S).lower()} --if_DM true"
            else:
                raise ValueError('Unknown target: {}'.format(target))
        elif interface == 'siesta' or interface == 'abacus':
            cmd = ''
        elif interface == 'aims':
            cmd = f"{julia_interpreter} " \
                  f"{os.path.join(os.path.dirname(os.path.dirname(__file__)), 'preprocess', 'aims_get_data.jl')} " \
                  f"--input_dir {input_dir} --output_dir {output_dir} --save_overlap {str(get_S).lower()}"
        else:
            raise ValueError('Unknown interface: {}'.format(interface))
        return cmd

    os.chdir(raw_dir)
    relpath_list = []
    writepath_list = []
    abspath_list = []
    for root, dirs, files in os.walk('./'):
        if (interface == 'openmx' and 'openmx.scfout' in files) or (
            interface == 'abacus' and 'OUT.' + abacus_suffix in dirs) or (
            interface == 'siesta' and any(['.HSX' in ifile for ifile in files])) or (
            interface == 'aims' and 'NoTB.dat' in files):
            
            if root.count('/') > 1:
                writepath = root.split('/')[1]
            else:
                writepath = copy(root)
            relpath_list.append(root)
            writepath_list.append(writepath)
            
            abspath_list.append(os.path.abspath(root))

    os.makedirs(processed_dir, exist_ok=True)
    os.chdir(processed_dir)
    print(f"Found {len(abspath_list)} directories to preprocess")

    def worker(index):
        time_cost = time.time() - begin_time
        current_block = index // nodes
        if current_block < 1:
            time_estimate = '?'
        else:
            num_blocks = (len(abspath_list) + nodes - 1) // nodes
            time_estimate = time.localtime(time_cost / (current_block) * (num_blocks - current_block))
            time_estimate = time.strftime("%H:%M:%S", time_estimate)
        print(f'\rPreprocessing No. {index + 1}/{len(abspath_list)} '
              f'[{time.strftime("%H:%M:%S", time.localtime(time_cost))}<{time_estimate}]...', end='')
        abspath = abspath_list[index]
        relpath = relpath_list[index]
        writepath = writepath_list[index]
        os.makedirs(writepath, exist_ok=True)
        cmd = make_cmd(
            abspath,
            os.path.abspath(writepath),
            target=target,
            interface=interface,
            get_S=get_S,
        )
        capture_output = sp.run(cmd, shell=True, capture_output=True, encoding="utf-8")
        if capture_output.returncode != 0:
            with open(os.path.join(os.path.abspath(relpath), 'error.log'), 'w') as f:
                f.write(f'[stdout of cmd "{cmd}"]:\n\n{capture_output.stdout}\n\n\n'
                        f'[stderr of cmd "{cmd}"]:\n\n{capture_output.stderr}')
            print(f'\nFailed to preprocess: {abspath}, '
                  f'log file was saved to {os.path.join(os.path.abspath(relpath), "error.log")}')
            return

        if interface == 'abacus':
            print("Output subdirectories:", "OUT." + abacus_suffix)
            abacus_parse(abspath, os.path.abspath(writepath), 'OUT.' + abacus_suffix)
        elif interface == 'siesta':
            siesta_parse(abspath, os.path.abspath(relpath))
        if local_coordinate:
            get_rc(os.path.abspath(relpath), os.path.abspath(relpath), radius=config.getfloat('graph', 'radius'),
                   r2_rand=config.getboolean('graph', 'r2_rand'),
                   create_from_DFT=config.getboolean('graph', 'create_from_DFT'), neighbour_file='hamiltonians.h5')
            get_rh(os.path.abspath(relpath), os.path.abspath(relpath), target)
        if config.getboolean('magnetic_moment', 'parse_magnetic_moment'):
            num_atom = np.loadtxt(os.path.join(os.path.abspath(relpath), 'element.dat')).shape[0]
            if interface == 'openmx':
                collect_magmom_from_openmx(
                    abspath, os.path.abspath(relpath),
                    num_atom, eval(config.get('magnetic_moment', 'magnetic_element')))
            elif interface == 'abacus':
                collect_magmom_from_abacus(
                    abspath, os.path.abspath(relpath), abacus_suffix,
                    num_atom, eval(config.get('magnetic_moment', 'magnetic_element')))
            else:
            	raise ValueError('Magnetic moment can only be parsed from OpenMX or ABACUS output for now, but your interface is {}'.format(interface))

    begin_time = time.time()
    if multiprocessing != 0:
        if multiprocessing > 0:
            pool_dict = {'nodes': multiprocessing}
        else:
            pool_dict = {}
        with Pool(**pool_dict) as pool:
            nodes = pool.nodes
            print(f'Use multiprocessing (nodes = {nodes})')
            pool.map(worker, range(len(abspath_list)))
    else:
        nodes = 1
        for index in range(len(abspath_list)):
            worker(index)
    print(f'\nPreprocess finished in {time.time() - begin_time:.2f} seconds')

if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Exercise the Matrix QMI8658 driver in a private Quad X SITL instance."""
import argparse
import math
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from pymavlink import mavutil

MAV = mavutil.mavlink


def verify(link):
    if not link.wait_heartbeat(timeout=25):
        raise RuntimeError("no heartbeat")
    sysid, compid = link.target_system, link.target_component
    for name in ("INS_ACC_ID", "INS_GYR_ID"):
        link.mav.param_request_read_send(sysid, compid, name.encode(), -1)
        msg = link.recv_match(type="PARAM_VALUE", condition=f'PARAM_VALUE.param_id=="{name}"',
                              blocking=True, timeout=8)
        if not msg or int(msg.param_value) != 0x456b01:
            raise RuntimeError(f"{name}: expected QMI8658 on bus 0 at 0x6b, got {msg}")
    def interval(msg_id):
        link.mav.command_long_send(sysid, compid, MAV.MAV_CMD_SET_MESSAGE_INTERVAL,
                                   0, msg_id, 50000, 0, 0, 0, 0, 0)
    interval(MAV.MAVLINK_MSG_ID_SCALED_IMU)
    interval(MAV.MAVLINK_MSG_ID_ATTITUDE)
    samples = []
    for _ in range(40):
        imu = link.recv_match(type="SCALED_IMU", blocking=True, timeout=4)
        if imu is None:
            raise RuntimeError("IMU stream stalled")
        samples.append(imu)
    if samples[-1].time_boot_ms <= samples[0].time_boot_ms:
        raise RuntimeError("frozen IMU timestamps")
    for imu in samples[-10:]:
        if not (-1050 < imu.zacc < -950 and abs(imu.xacc) < 40 and abs(imu.yacc) < 40
                and max(abs(imu.xgyro), abs(imu.ygyro), abs(imu.zgyro)) < 30):
            raise RuntimeError(f"stationary IMU outside expected range: {imu}")
    att = link.recv_match(type="ATTITUDE", blocking=True, timeout=4)
    if not att or max(abs(att.roll), abs(att.pitch)) > math.radians(5):
        raise RuntimeError(f"unexpected level attitude: {att}")
    print(f"QMI8658 gyro and accel IDs 0x456b01, {len(samples)} messages, "
          f"stationary Z {samples[-1].zacc} mg, roll/pitch "
          f"{math.degrees(att.roll):.2f}/{math.degrees(att.pitch):.2f} deg")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    board = Path(__file__).resolve().parent
    defaults = board.parents[3] / "Tools/autotest/default_params/copter.parm"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryFile(mode="w+") as log:
        proc = subprocess.Popen([str(args.binary.resolve()), "--model", "x",
                                 "--base-port", str(port), "--instance", "37",
                                 "--defaults", f"{defaults},{board / 'defaults.parm'}"],
                                cwd=tmp, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 20
            while proc.poll() is None and time.monotonic() < deadline:
                try:
                    link = mavutil.mavlink_connection(f"tcp:127.0.0.1:{port}", retries=0)
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("SITL did not open its MAVLink port")
            try:
                verify(link)
            finally:
                link.close()
        except Exception:
            log.seek(0)
            print(log.read()[-4000:])
            raise
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    main()

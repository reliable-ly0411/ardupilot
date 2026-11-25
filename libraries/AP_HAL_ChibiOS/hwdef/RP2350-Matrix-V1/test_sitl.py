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


def verify(link, flight):
    if not link.wait_heartbeat(timeout=25):
        raise RuntimeError("no heartbeat")
    sysid, compid = link.target_system, link.target_component
    for name in ("INS_ACC_ID", "INS_GYR_ID"):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            link.mav.param_request_read_send(sysid, compid, name.encode(), -1)
            msg = link.recv_match(type="PARAM_VALUE", condition=f'PARAM_VALUE.param_id=="{name}"',
                                  blocking=True, timeout=2)
            if msg and int(msg.param_value) == 0x456b01:
                break
            time.sleep(0.25)
        else:
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

    if not flight:
        return
    # This link belongs to the simulator process created below.
    interval(MAV.MAVLINK_MSG_ID_GLOBAL_POSITION_INT)

    def command(cmd, *args):
        params = list(args) + [0] * (7-len(args))
        link.mav.command_long_send(sysid, compid, cmd, 0, *params)

    deadline = time.monotonic() + 85
    next_arm = 0
    while time.monotonic() < deadline:
        hb = link.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
        if hb and hb.base_mode & MAV.MAV_MODE_FLAG_SAFETY_ARMED:
            break
        if time.monotonic() >= next_arm:
            if link.flightmode != "GUIDED":
                link.set_mode_apm("GUIDED")
            else:
                command(MAV.MAV_CMD_COMPONENT_ARM_DISARM, 1)
            next_arm = time.monotonic() + 2
    else:
        raise RuntimeError(f"virtual quad did not arm: {link.messages.get('STATUSTEXT')}")
    command(MAV.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 3)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        pos = link.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=1)
        if pos and pos.relative_alt >= 2500:
            print(f"Quad X takeoff reached {pos.relative_alt/1000:.2f} m")
            break
    else:
        raise RuntimeError("virtual quad did not reach 2.5 m")
    link.set_mode_apm("LAND")
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        hb = link.recv_match(type="HEARTBEAT", blocking=True, timeout=2)
        if hb and not hb.base_mode & MAV.MAV_MODE_FLAG_SAFETY_ARMED:
            print("Quad X landed and disarmed")
            break
    else:
        raise RuntimeError("virtual quad did not disarm on landing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--flight", action="store_true", help="also take off and land in SITL")
    args = parser.parse_args()
    board = Path(__file__).resolve().parent
    defaults = board.parents[3] / "Tools/autotest/default_params/copter.parm"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryFile(mode="w+") as log:
        # The generic quad fixture pre-calibrates two IMUs. Matrix fits one.
        quad_defaults = Path(tmp) / "quad.parm"
        quad_defaults.write_text("\n".join(line for line in defaults.read_text().splitlines()
                                           if not line.startswith(("INS_ACC2OFFS", "INS_ACC2SCAL"))))
        proc = subprocess.Popen([str(args.binary.resolve()), "--model", "x",
                                 "--base-port", str(port), "--instance", "37",
                                 "--defaults", f"{quad_defaults},{board / 'defaults.parm'}"],
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
                verify(link, args.flight)
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

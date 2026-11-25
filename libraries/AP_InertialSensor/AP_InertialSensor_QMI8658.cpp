#include "AP_InertialSensor_QMI8658.h"

#if AP_INS_QMI8658_ENABLED
#include <utility>

extern const AP_HAL::HAL &hal;

namespace {
enum Register : uint8_t {
    WHO_AM_I = 0x00, CTRL1 = 0x02, CTRL2 = 0x03, CTRL3 = 0x04,
    CTRL7 = 0x08, CTRL8 = 0x09, CTRL9 = 0x0a, CAL1_L = 0x0b,
    STATUSINT = 0x2d, TEMP_L = 0x33, RESET = 0x60,
};

int16_t signed_le16(const uint8_t *p)
{
    return int16_t(p[0] | (uint16_t(p[1]) << 8));
}
}

AP_InertialSensor_QMI8658::AP_InertialSensor_QMI8658(AP_InertialSensor &imu,
    AP_HAL::OwnPtr<AP_HAL::I2CDevice> device, Rotation mounting) :
    AP_InertialSensor_Backend(imu), dev(std::move(device)), rotation(mounting)
{}

AP_InertialSensor_Backend *AP_InertialSensor_QMI8658::probe(AP_InertialSensor &imu,
    AP_HAL::OwnPtr<AP_HAL::I2CDevice> device, Rotation mounting)
{
    if (!device) {
        return nullptr;
    }
    auto *sensor = NEW_NOTHROW AP_InertialSensor_QMI8658(imu, std::move(device), mounting);
    if (sensor && !sensor->configure()) {
        delete sensor;
        return nullptr;
    }
    return sensor;
}

bool AP_InertialSensor_QMI8658::wait_command(bool done)
{
    for (uint8_t i = 0; i < 50; i++) {
        uint8_t status;
        if (dev->read_registers(STATUSINT, &status, 1) && bool(status & 0x80) == done) {
            return true;
        }
        hal.scheduler->delay(1);
    }
    return false;
}

bool AP_InertialSensor_QMI8658::configure()
{
    WITH_SEMAPHORE(dev->get_semaphore());
    uint8_t id;
    if (!dev->read_registers(WHO_AM_I, &id, 1) || id != 0x05 ||
        !dev->write_register(RESET, 0xb0)) {
        return false;
    }
    hal.scheduler->delay(30);
    // QST rev D, sections 5.10 and 11. Disable AHB gating before locking I2C samples.
    return dev->write_register(CTRL1, 0x40) && // little endian, address increment
           dev->write_register(CTRL7, 0) &&
           dev->write_register(CTRL8, 0x80) &&
           dev->write_register(CAL1_L, 1) &&
           dev->write_register(CTRL9, 0x12) && wait_command(true) &&
           dev->write_register(CTRL9, 0) && wait_command(false) &&
           dev->write_register(CTRL2, 0x33) && // 16 g, 896.8 Hz
           dev->write_register(CTRL3, 0x73) && // 2048 deg/s, 896.8 Hz
           dev->write_register(CTRL7, 0x83);   // synchronized gyro and accel
}

void AP_InertialSensor_QMI8658::start()
{
    const uint32_t id = dev->get_bus_id_devtype(DEVTYPE_INS_QMI8658);
    if (!_imu.register_gyro(gyro_instance, 897, id) ||
        !_imu.register_accel(accel_instance, 897, id)) {
        return;
    }
    set_gyro_orientation(gyro_instance, rotation);
    set_accel_orientation(accel_instance, rotation);
    dev->register_periodic_callback(700, FUNCTOR_BIND_MEMBER(&AP_InertialSensor_QMI8658::read_sample, void));
}

void AP_InertialSensor_QMI8658::read_sample()
{
    uint8_t status;
    if (!dev->read_registers(STATUSINT, &status, 1) || !(status & 1)) {
        return;
    }
    // Reading STATUSINT latches the sample. Lock delay at this ODR is 6 us.
    if (!(status & 2)) {
        hal.scheduler->delay_microseconds(6);
    }
    uint8_t raw[14];
    if (!dev->read_registers(TEMP_L, raw, sizeof(raw))) {
        return;
    }
    // The burst ends at GZ_H, releasing the latch for the next sample.
    Vector3f accel(signed_le16(raw+2), signed_le16(raw+4), signed_le16(raw+6));
    Vector3f gyro(signed_le16(raw+8), signed_le16(raw+10), signed_le16(raw+12));
    accel *= GRAVITY_MSS / 2048.0f;
    gyro *= radians(1.0f) / 16.0f;
    _rotate_and_correct_accel(accel_instance, accel);
    _rotate_and_correct_gyro(gyro_instance, gyro);
    const uint64_t now = AP_HAL::micros64();
    _notify_new_accel_raw_sample(accel_instance, accel, now);
    _notify_new_gyro_raw_sample(gyro_instance, gyro, now);
    _publish_temperature(accel_instance, signed_le16(raw) / 256.0f);
}

bool AP_InertialSensor_QMI8658::update()
{
    update_gyro(gyro_instance);
    update_accel(accel_instance);
    return true;
}
#endif

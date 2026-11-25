#pragma once

#include <AP_HAL/AP_HAL_Boards.h>
#if AP_INS_QMI8658_ENABLED
#include "AP_InertialSensor_Backend.h"
#include <AP_HAL/I2CDevice.h>

class AP_InertialSensor_QMI8658 : public AP_InertialSensor_Backend {
public:
    static AP_InertialSensor_Backend *probe(AP_InertialSensor &imu,
        AP_HAL::OwnPtr<AP_HAL::I2CDevice> dev, Rotation rotation);
    void start() override;
    bool update() override;

private:
    AP_InertialSensor_QMI8658(AP_InertialSensor &imu, AP_HAL::OwnPtr<AP_HAL::I2CDevice> dev,
                              Rotation rotation);
    bool configure();
    bool wait_command(bool done);
    void read_sample();
    AP_HAL::OwnPtr<AP_HAL::I2CDevice> dev;
    Rotation rotation;
    uint8_t accel_instance;
    uint8_t gyro_instance;
};
#endif

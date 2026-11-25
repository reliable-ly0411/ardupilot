#pragma once

#include "SIM_config.h"
#if AP_SIM_QMI8658_ENABLED
#include "SIM_I2CDevice.h"

namespace SITL {
// Register subset used by the Matrix driver. Samples follow the quad body model.
class QMI8658 : public I2CDevice {
public:
    int rdwr(I2C::i2c_rdwr_ioctl_data *&data) override;

private:
    uint8_t registers[16] {};
    bool locked {};
    bool gating_disabled {};
    uint64_t sample_time {};
    int16_t sample[7] {};
};
}
#endif

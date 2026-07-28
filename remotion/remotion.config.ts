import {Config} from '@remotion/cli/config';

Config.setCodec('h264');
Config.setPixelFormat('yuv420p');
Config.setOverwriteOutput(true);
Config.setConcurrency(2);

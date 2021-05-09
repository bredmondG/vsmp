#!/usr/bin/python
# -*- coding:utf-8 -*-
import sys
import os
import ffmpeg
import logging
import epd7in5
import time
from threading import Thread
from PIL import Image,ImageDraw,ImageFont
import traceback
import pickle
from pathlib import Path
import subprocess
        
logging.basicConfig(filename='log.txt', filemode='w', level=logging.DEBUG)
logging.warning('hello log')

def slice_video():
    #cuts yojimbo into 20 equal sections
    video = 'YOJIMBO.m4v'
    s = os.popen('ffprobe -v error -select_streams v:0 -show_entries stream=duration -of default=noprint_wrappers=1:nokey=1 {}'.format(video))
    seconds = float(s.read().split()[0])
    duration = seconds/20
    t = 0
    for i, section in enumerate(range(0,10)):
        print(t, t + duration)
        command = 'ffmpeg -i YOJIMBO.m4v -ss {} -t {} -c copy yojimbo/yojimbo_section{}.m4v'.format(t, duration, i)
        os.popen(command)
        t+= duration
        
def frame_count(clip):
    fast_count = 'ffprobe -v error -select_streams v:0 -show_entries stream=nb_frames -of default=nokey=1:noprint_wrappers=1 yojimbo/{}'.format(clip)
    s = os.popen(
                fast_count
                ).read()
    frames = int(s.split()[0])
    return frames
        
def generate_frames(clip, frame, frame_len):
    
    while frame < frame_len:
        if len(os.listdir('yojimbo_frames')) < 3000:
            if ('out_img%d.jpg' %(frame)) not in os.listdir('yojimbo_frames'):
                os.system('ffmpeg -i yojimbo/{} -vf "select=gte(n\,{})" -vframes 1 yojimbo_frames/out_img{}.jpg'.format(clip,frame, frame))
            frame +=1

    logging.info("generate_frames done")
    
def generate_frame(clip, frame):
    if 'out_img{}.jpg'.format(frame) not in os.listdir('yojimbo_frames'):
        os.popen('ffmpeg -i yojimbo/{} -vf "select=gte(n\,{})" -vframes 1 yojimbo_frames/out_img{}.jpg'.format(clip,frame, frame)).read()
        logging.info("generated_frame: {}".format(frame))
        
        
def display_frame(clip, frame, frame_len, progress):
    try:        
        epd = epd7in5.EPD()
        logging.info("init and Clear")
        epd.init()
        epd.Clear()
        #time.sleep(1)
        logging.info("Starting({}, {}): ".format(clip, frame))
        while frame < frame_len:
            start_t = time.time()
            logging.info("section: {}".format(clip))
            logging.info("frames in section: %d" %frame_len)
            logging.info("Frame: {}".format(frame))
            generate_frame(clip,frame)
            frame_path = 'yojimbo_frames/out_img%d.jpg' % (frame)
            if Path(frame_path).exists():
                im = Image.open(os.path.join('yojimbo_frames/out_img%d.jpg' % (frame)))
                sized = im.resize((640,384), Image.ANTIALIAS)
                epd.display(epd.getbuffer(sized))
                os.remove(frame_path)
                frame += 1
                progress['frame'] = frame
                save_data('progress.pkl', progress)
            else:
                logging.info("{} not in yojimbo_frames".format(frame_path))
            end_t = time.time()
            lapse = end_t - start_t
            if lapse < 150:
                logging.info("Time to generate: {}s".format(round(lapse, 2)))
                time.sleep(150 - lapse)
            else:
                logging.info("Time to Generate greater than 2.5 minutes")
            logging.info(time.asctime(time.localtime(time.time())))
        logging.info("finished section: {}".format(clip))
        epd.Clear()
        

    except IOError as e:
        logging.info(e)
        
    except KeyboardInterrupt:    
        logging.info("ctrl + c:")
        epd7in5bc.epdconfig.module_exit()
        exit()

def save_data(file, data):
    with open(file, 'wb') as f:
        pickle.dump(data, f)

def load_data(file, data):
    if Path(file).exists():
        with open(file, 'rb') as f:
            return pickle.load(f)
    return data

def play_movie():
    progress = load_data('progress.pkl', {
                            'sections' : os.listdir('yojimbo'),
                            'sections_ran': ['yojimbo_section0.m4v','yojimbo_section1.m4v','yojimbo_section2.m4v'],
                            'frame' : 0
                            }
                         )
    logging.info("Playing Movie at frame: {}".format(progress['frame']))
    for i in range(0, len(progress['sections'])):
        clip = 'yojimbo_section{}.m4v'.format(i)
        if clip not in progress['sections_ran']:
            frame = progress['frame']
            frame_len = frame_count(clip)- 5
            logging.info("Section: {}".format(clip))
            display_frame(clip, frame, frame_len, progress)
            progress['sections_ran'].append(clip)
            progress['frame'] = 0
            save_data('progress.pkl', progress)
        else:
            logging.info("Already Ran: {}".format(clip))
    logging.info("movie finished")
    epd.sleep()
if __name__ == '__main__':
    play_movie()



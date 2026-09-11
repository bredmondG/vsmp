#!/usr/bin/python
# -*- coding:utf-8 -*-
import os
import logging
from epd import epd7in5_V2
from epd import epd7in5bc
import time
from threading import Thread
from PIL import Image,ImageDraw,ImageFont
import pickle
from pathlib import Path

        
logging.basicConfig(filename='log.txt', filemode='w', level=logging.DEBUG)
logging.warning('hello log')

        
def create_text_on_multiple_lines(text_lines: list, image_path, 
                                  image_size=(800, 480), 
                                  font_size=22, font_path = "FreeSansBold.ttf"):
    img = Image.new('RGB', image_size, color=(255, 255, 255))
    
    # Initialize drawing context
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(font_path, font_size)
    except IOError:
        font = ImageFont.load_default()  # Use default font if font file not found
    i = 0
    # Heights of text lines, height of e-ink display is 480
    heights = list(range(20, 460, 40))
    for text in text_lines:
        position = (24, heights[i])
        i += 1
        draw.text(position, text, font=font, fill=(0, 0, 0))

    img.save(image_path)
    

# Function to create a .jpg image with text
def create_text_image(text, image_path, image_size=(800, 480), font_size=40):
    font_type = "FreeSansBold.ttf"

    # Create a blank image with white background
    img = Image.new('RGB', image_size, color=(255, 255, 255))
    
    # Initialize drawing context
    draw = ImageDraw.Draw(img)
    
    # Load a font (you can use a system font or a TTF file)
    try:
        font = ImageFont.truetype(font_type, font_size)
    except IOError:
        logging.error(f"{font_type} not found using default")
        font = ImageFont.load_default()  # Use default font if arial.ttf is not available
    
    # Calculate the width and height of the text to be drawn, based on font size
    text_width, text_height = draw.textsize(text, font=font)
    
    # Calculate the position to center the text
    position = ((image_size[0] - text_width) // 2, (image_size[1] - text_height) // 2)
    
    # Draw the text onto the image (black color text)
    draw.text(position, text, font=font, fill=(0, 0, 0))
    
    # Save the image as a .jpg file
    img.save(image_path, "JPEG")

def show_image(image_path, epd):
    im = Image.open(image_path).resize((800,480))
    epd.display(epd.getbuffer(im))
    logging.info(time.asctime(time.localtime(time.time())))
    logging.info("sleeping 150")
    time.sleep(150)
    os.remove(image_path)

def display_line(lines, line_number, epd):
    logging.info(f"Displaying line: {line_number}")
    image_path = f"line_number_{line_number}.jpg"
    create_text_on_multiple_lines(lines, image_path)
    show_image(image_path, epd)

def save_data(file, data):
    with open(file, 'wb') as f:
        pickle.dump(data, f)

def load_data(file, data):
    if Path(file).exists():
        with open(file, 'rb') as f:
            return pickle.load(f)
    return data


def play_text(epd, text):
    progress = load_data('bookmark.pkl', {
                            'number_of_lines' : len(text),
                            'line_number' : 0
                            }
                         )
    line_number = progress['line_number']
    logging.info(f"displaying book at line: {line_number}")
    logging.info(f"number of lines in text: {progress['number_of_lines']}")
    while line_number < progress['number_of_lines']:
        lines = text[line_number: line_number + 9]
        display_line(lines, line_number, epd)
        line_number += 1
        progress['line_number'] = line_number
        save_data('bookmark.pkl', progress)
    logging.info("text finished")
    epd.sleep()

def open_txt_file(file_path):
    with open(file_path, "r") as file:
        file_content = file.read()
        return file_content

def process_text(text):
    # start text a few lines later to remove non text characters
    return text.split("\n")[2:]

if __name__ == '__main__':
    try:        
        epd = epd7in5_V2.EPD()
        logging.info("init and Clear")
        epd.init()
        epd.Clear()
        text = open_txt_file("/home/brendangold/development/vsmp/pg4300.txt")
        processed_text = process_text(text)
        play_text(epd, processed_text)

        
    except IOError as e:
        raise Exception(logging.info(e))
        
    except KeyboardInterrupt:    
        logging.info("ctrl + c:")
        epd7in5_V2.epdconfig.module_exit()
        exit()



import cv2
import os.path
import numpy as np
import scipy.stats as ss
from PIL import Image
from pywt import dwt2
import random
import math

# Set up logging
from utils.customlogger import CustomLogger
main_logger = CustomLogger().main_logger

def _count_colours(image : cv2.typing.MatLike):
    """
    Get the number of unique colours and the percentage of the primary colour in the given image.
    """
    
    # Flatten the image to a 2D array
    flattend_image = image.reshape(-1, image.shape[-1])
    
    # Get the unique colors and the number of times each appears in the image
    unique_colors, unique_colors_pixels = np.unique(flattend_image, axis = 0, return_counts = True) 
    
    primary_color_percentage = np.amax(unique_colors_pixels, initial = 0) / max(np.sum(unique_colors_pixels), 1) * 100
    
    return len(unique_colors), primary_color_percentage


def _draw_regions(image: cv2.typing.MatLike, img_path: str, regions: list, highlight_name: str):
    """
    Draw the detected regions on the originial image.
    """

    draw_img = np.copy(image)
    
    for index, region in enumerate(regions):
        main_logger.debug(f"Drawing region #{index}")
        
        region_height, region_width, _ = region[0].shape
        x = region[2]
        y = region[3]
        
        color_int = random.randint(0, 2)
        colors = [(0,0,255), (0,255,0), (255,0,0)]
        color = colors[color_int]
        
        flip = (random.randint(0, 1) == 1)
        
        cv2.rectangle(draw_img, (x - 5, y - 5), (x + region_width - 5, y + region_height - 5), color, 1)
        
        if region[7]:
            text = "-" + str(region[1])
        else:
            text = str(region[1])
            
        if flip:
            cv2.putText(draw_img, text, (x + region_width - random.randint(-5, 5), y + region_height-random.randint(-5, 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        else:
            cv2.putText(draw_img, text, (x - random.randint(-5, 5), y - random.randint(-5, 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        #TODO: Add subregion drawing
        # if subregion_draw:
        #     cv2.imwrite(f"{highlight_name}.subregion.{index}.png", region[0])
            
    cv2.imwrite(os.path.join(os.path.dirname(os.path.realpath(img_path)), f"{highlight_name}.png"), draw_img)

# TODO: Add drawing capabilities for recursive and try to split the function into smaller parts
def _find_regions(image : cv2.typing.MatLike, image_path : str, draw : bool, highlight_name : str, invert = True):
    
    main_logger.debug("Obtaining grayscale version of image")
    img = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    
    main_logger.debug("Thresholding the image")
    if invert:
        cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU, img)
    else:
        cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU, img)
        
    main_logger.debug("Dilating")
    img = cv2.dilate(img, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 5)), iterations = 1)

    main_logger.debug("Morphing to merge close area's")
    img = cv2.morphologyEx(img, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)))
    
    main_logger.debug("Eroding")
    img = cv2.erode(img, cv2.getStructuringElement(cv2.MORPH_RECT, (4, 4)), iterations = 1)
    
    main_logger.debug("Finding contours")
    contours, hier = cv2.findContours(img, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    
    main_logger.debug("Storing valid contours")
    
    regions = []

    if len(contours) != 0:
        for index, contour in enumerate(contours):
            [x, y, w, h] = cv2.boundingRect(contour)

            # Adding small padding to image for slight context and better search accuracy
            margin = 5
            region = image[max(0, y - margin) : y + h + margin, max(0, x - margin) : x + w + margin]

            # Always true - region constraints could be applied here
            
            unique_colors_count, pct = _count_colours(region)
            # also get a greyscale version of the region for the other attributes
            # (see paper by Evdoxios Baratis and Euripides G.M. Petrakis why this is)

            if (region.size == 0):
                    continue
            r_grey = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)

            # Image info
            mean = np.mean(r_grey, axis = None)
            std = np.std(r_grey, axis = None)
            skew = ss.skew(r_grey, axis = None)
            kurtosis = ss.kurtosis(r_grey, axis = None)
            entropy = ss.entropy(r_grey, axis = None)

            #Otsu threshold
            otsu = 0
            if invert:
                otsu = cv2.threshold(r_grey, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[0]
            else:
                otsu = cv2.threshold(r_grey, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0]

            # Energy
            _, (cH, cV, cD) = dwt2(r_grey.T, 'db1')
            energy = (cH**2 + cV**2 + cD**2).sum()/r_grey.size
            
            if math.isnan(energy):
                energy = 0.0
                
            # Number of shades of grey
            int_hist = cv2.calcHist([r_grey], [0], None, [256], [0, 256]).flatten()
            occupied_bins = np.count_nonzero(int_hist)

            if len(hier) > 0:
                regions.append((region, index, x, y, unique_colors_count, pct, hier[0][index], invert, mean, std, skew, kurtosis, entropy, otsu, energy, occupied_bins))
            else:
                regions.append((region, index, x, y, unique_colors_count, pct, [-2, -2, -2, -2], invert, mean, std, skew, kurtosis, entropy, otsu, energy, occupied_bins))
            
    return regions

def _validate_regions(regions : list):
    regions_of_interest = []
    
    for index, region in enumerate(regions):
        region_height, region_width, _ = region[0].shape

        for index2, region2 in enumerate(regions):
            # Skip the same region. We do not want to compare the same region with itself
            if index == index2:
                continue
            
            region_height2, region_width2, _ = region2[0].shape
            if region[2] >= region2[2] and (region[2] + region_width <= region2[2] + region_width2):
                # On x axis region1 is contained within region2
                if region[3] >= region2[3] and (region[3] + region_height <= region2[3] + region_height2):
                    # On y axis region 1 is contained within region2
                    continue
        
        regions_of_interest.append(region)
        
    return regions_of_interest

FLAG_NO_DRAW = 0
"""Tells the function to **NOT** draw any debugging images.""" 

FLAG_DRAW = 1
"""Tells the function to draw the regions on the original image.""" 

FLAG_DRAW_RECUSRIVE = 2
"""Tells the function to draw the regions and subregions.""" 

def find_regions (image_path : str, draw_flag = FLAG_DRAW, highlight_name = "Highlight"):
    """
    Finds all the regions of interest in the image and returns the data of the image.
    
    Args:
        img_path (str): The path (location) of the image.
        draw_flag (int, optional): The flag to tell the function to draw the regions. Defaults to FLAG_DRAW.
        highlight_name (str, optional): The name of the file to save the highlighted image. Defaults to "Highlight".
    """
    
    if(draw_flag < FLAG_NO_DRAW or draw_flag > FLAG_DRAW_RECUSRIVE):
        raise ValueError("Invalid draw_flag")
        
    draw = draw_flag != FLAG_NO_DRAW
    recursive_draw = draw_flag == FLAG_DRAW_RECUSRIVE
    
    main_logger.debug("Loading image: " + image_path)
    image = cv2.imread(image_path, cv2.IMREAD_COLOR)
    
    img_data = (_count_colours(image), image.shape[0], image.shape[1])
    
    regions = _find_regions(image, image_path, recursive_draw, highlight_name = f"{highlight_name}.allregions.inverted", invert = True)
    regions += _find_regions(image, image_path, recursive_draw, highlight_name = f"{highlight_name}.allregions.not_inverted", invert = False)
    
    regions_of_interest = _validate_regions(regions)
        
    if draw:
        _draw_regions(image, image_path, regions_of_interest, f"{highlight_name}.allregions")

    return regions_of_interest, img_data
    
# def _findregions(image, imgpath, draw = True, highlight_name="Highlight", invert=True):
#     """
#     This function is deprecated. Use _find_regions() instead.
#     ------
#     """
    
#     _find_regions(image, imgpath, draw, highlight_name, invert)
    
    # if draw:
    #     drawimg = np.copy(image)

    # main_logger.debug("Obtaining grayscale version of image")
    # img = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # if draw:
    #     cv2.imwrite(f"{highlight_name}-0-grey.png", img)
        
    # main_logger.debug("Thresholding the image")
    # if invert:
    #     cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,img)
    # else:
    #     cv2.threshold(img, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU,img)
    
    # if draw:
    #     cv2.imwrite(f"{highlight_name}-0-tresh.png", img)

    # main_logger.debug("Dilating")
    # img = cv2.dilate(img, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 5)), iterations=1)
    # if draw:
    #     cv2.imwrite(f"{highlight_name}-1-dilating.png", img)

    # main_logger.debug("Morphing to merge close area's")
    # #img = cv2.morphologyEx(img, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 4)))
    # img = cv2.morphologyEx(img, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (5,5)))
    
    # if draw:
    #     cv2.imwrite(f"{highlight_name}-2-inter.png", img)

    # main_logger.debug("Eroding")
    # img = cv2.erode(img, cv2.getStructuringElement(cv2.MORPH_RECT, (4, 4)), iterations=1);
    # if draw:
    #     cv2.imwrite(f"{highlight_name}-3-eroding.png", img)

    # main_logger.debug("Finding contours")
    # contours, hier = cv2.findContours(img, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    # main_logger.debug("Storing valid contours")
    # roi = []
    # count = 0
    # if len(contours) != 0:
    #     for i,c in enumerate(contours):
    #         [x,y,w,h] = cv2.boundingRect(c)

    #         # Adding small padding to image for slight context and better search accuracy
    #         margin=5
    #         r = image[max(0, y-margin):y+h+margin, max(0, x-margin):x+w+margin]

    #         image_width, image_height = Image.open(imgpath).size

    #         # Always true - region constraints could be applied here
            
    #         ccnt, pct = _count_colours(r)
    #         # also get a greyscale version of the region for the other attributes
    #         # (see paper by Evdoxios Baratis and Euripides G.M. Petrakis why this is)

    #         if (r.size == 0):
    #                 continue
    #         r_grey = cv2.cvtColor(r, cv2.COLOR_BGR2GRAY)

    #         # Image info
    #         mean = np.mean(r_grey, axis=None)
    #         std = np.std(r_grey, axis=None)
    #         skew = ss.skew(r_grey, axis=None)
    #         kurtosis = ss.kurtosis(r_grey, axis=None)
    #         entropy = ss.entropy(r_grey, axis=None)

    #         #Otsu threshold
    #         otsu = 0
    #         if invert:
    #             otsu = cv2.threshold(r_grey, 0, 255, cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[0]
    #         else:
    #             otsu = cv2.threshold(r_grey, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)[0]

    #         # Energy
    #         _, (cH, cV, cD) = dwt2(r_grey.T, 'db1')
    #         energy = (cH**2 + cV**2 + cD**2).sum()/r_grey.size
    #         if math.isnan(energy):
    #             energy = 0.0
    #         # Number of shades of grey
    #         int_hist = cv2.calcHist([r_grey], [0], None, [256], [0, 256]).flatten()
    #         occupied_bins = np.count_nonzero(int_hist)
    #         if draw:
    #             cv2.rectangle(drawimg,(x-margin,y-margin),(x+w+margin,y+h+margin),(0,0,255),1)

    #         if len(hier) > 0:
    #             roi.append((r, i, x, y, ccnt, pct, hier[0][i], invert, mean, std, skew, kurtosis, entropy, otsu, energy, occupied_bins))
    #         else:
    #             roi.append((r, i, x, y, ccnt, pct, [-2, -2, -2, -2], invert, mean, std, skew, kurtosis, entropy, otsu, energy, occupied_bins))
    #         count += 1
            
    # if draw:
    #     cv2.imwrite(f"{highlight_name}.png", drawimg)
    #     main_logger.debug("Wrote image highlighting the regions to: " + highlight_name)
        
    # return roi
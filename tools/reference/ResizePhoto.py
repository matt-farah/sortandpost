# Reference only (V3 "Instagram photo resizing"): the user's original fit-and-pad
# script, kept as the spec for the posting pipeline. Not imported by the app.
from PIL import Image, ImageOps
import os

#set the folder path to process
folderToProcess = "/path/to/folder"
#set the final image size
finalHeight = 1350
finalWidth = 1080
#set the border size
finalBorder = 10
#set the background color
finalCanvasR = 255
finalCanvasG = 255
finalCanvasB = 255

def process_image (input, output, targetWidth, targetHeight, borderSize, canvasR, canvasG, canvasB):
    # Load. the image
    image = Image.open(input).convert("RGB")
    originalWidth, originalHeight = image.size
    print ("originalw",originalWidth,"originalh", originalHeight)
    
    #resize the image
    ratio = min((targetWidth - borderSize )/ originalWidth, (targetHeight - borderSize) /originalHeight)
    print(ratio)
    newSize = (int(originalWidth * ratio), int (originalHeight * ratio))
    resizedImage = image.resize(newSize)

    #create a new white canvas and center the image
    canvas = Image.new('RGB', (targetWidth,targetHeight), (canvasR,canvasG,canvasB))
    offset_x = (targetWidth - resizedImage.width) // 2
    offset_y = (targetHeight - resizedImage.height) // 2
    canvas.paste (resizedImage, (offset_x, offset_y))

    canvas.save(output)
    print(f"Image saved to {output}")

with os.scandir(folderToProcess) as files:
    for file in files:
        name = file.name
        print("processing...", name)
        filePath = folderToProcess + "/" + name
        newFilePath = folderToProcess + "/" + "i_" + name
        print(filePath,newFilePath,finalWidth,finalHeight)
        process_image (filePath,newFilePath,finalWidth,finalHeight,finalBorder,finalCanvasR,finalCanvasG,finalCanvasB)


# process_image ("/path/to/photo.jpg", "/path/to/resized.jpg", 1080, 1350)
from __future__ import print_function

from time import sleep
from datetime import datetime, timedelta
import gphoto2 as gp
import signal,os,subprocess
import sys, getopt

###########################################################
##############  DEFAULT PARAMETER VALUES ##################
###########################################################
shutterspeed = 1.0	# shutterspeed in seconds
frames = 1			# one frame
label = 'Test'		# test frame by default
config = False		# print camera config
iso = -1			# iso not set
relative_delay = None	# delay between frames in seconds
absolute_period = None	# absolute frame boundary period in seconds

frame_date = datetime.now().strftime('%Y-%m-%d')
frame_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
picID = 'PI %y-%m-%d %H:%M:%S'

folder_name = frame_date
save_location = os.path.expanduser("~/Desktop/gphoto/images/" + folder_name)

def callback(level, domain, string, data=None):
        print('Callback: level =', level, ', domain =', domain, ', string =', string)
        if data:
            print('Callback data:', data)


def parseInterval(value):
	text = value.strip().lower()
	if text.endswith('s'):
		return float(text[:-1])
	if text.endswith('m'):
		return float(text[:-1]) * 60.0
	return float(text)


def waitUntil(target_time):
	while True:
		remaining = (target_time - datetime.now()).total_seconds()
		if remaining <= 0:
			return
		sleep(min(remaining, 1.0))


def getNextAbsoluteBoundary(period_seconds, reference_time=None):
	if reference_time is None:
		reference_time = datetime.now()
	epoch = datetime(1970, 1, 1)
	elapsed = (reference_time - epoch).total_seconds()
	next_tick = (int(elapsed // period_seconds) + 1) * period_seconds
	return epoch + timedelta(seconds=next_tick)


def waitForFrameTiming(frame_index):
	if absolute_period:
		target_time = getNextAbsoluteBoundary(absolute_period)
		print('Waiting for absolute boundary at '+target_time.strftime('%Y-%m-%d %H:%M:%S'))
		waitUntil(target_time)
	elif relative_delay and frame_index > 0:
		print('Waiting '+str(relative_delay)+'s before next frame')
		sleep(relative_delay)


# search for processes containing gphoto2 and kill them
# the python gphoto2 library cannot get access to the camera otherwise

def killgphoto2Process():
	p = subprocess.Popen(['ps', '-A'], stdout=subprocess.PIPE)
	out, err = p.communicate()
	
	for line in out.splitlines():
		if b'gphoto2' in line:
			# default split (None) is on whitespace
			p_id = int(line.split(None)[0]) 
			p_name = line.split(None)[3]
			print("Killing process ID:"+str(p_id)+" Name:"+str(p_name))
			os.kill(p_id, signal.SIGKILL)

# create a folder using date as part of the folder name
# used for transfering frames from the camera to the host

def createFramesFolder():
	try:
		os.makedirs(save_location)
	except FileExistsError:
		print("Using Frames Folder:"+save_location)
	except Exception as e:
		print("Failed to create frames folder:"+save_location)
		print(e)
		exit(1)
		
	os.chdir(save_location)
	
# captures a frame and transfers it to the host
# use this method if the camera is NOT set to 'bulb'
# bulb frames require two triggers (start + stop)

def captureFrame(camera):
	print('Capturing frame')
	file_path = camera.capture(gp.GP_CAPTURE_IMAGE)
	return file_path


def waitForCapturedFile(camera, file_path, timeout_seconds=60):
	deadline = datetime.now() + timedelta(seconds=timeout_seconds)
	while datetime.now() < deadline:
		try:
			getFileInfo(camera, os.path.join(file_path.folder, file_path.name))
			return
		except gp.GPhoto2Error:
			pass
		try:
			camera.wait_for_event(1000)
		except gp.GPhoto2Error:
			pass
		sleep(1)
	raise RuntimeError('Timed out waiting for captured file '+os.path.join(file_path.folder, file_path.name))
	
# downloads a single file from the camera, retrying while the camera is
# still finishing the write after capture

def saveCameraFile(camera, path, target, retries=10, delay=1):
	folder, name = os.path.split(path)
	file_types = [gp.GP_FILE_TYPE_RAW, gp.GP_FILE_TYPE_NORMAL]
	if hasattr(gp, 'GP_FILE_TYPE_AUTO'):
		file_types.insert(0, gp.GP_FILE_TYPE_AUTO)

	for attempt in range(retries):
		try:
			last_error = None
			for file_type in file_types:
				try:
					camera_file = camera.file_get(folder, name, file_type)
					camera_file.save(target)
					return
				except gp.GPhoto2Error as e:
					last_error = e
			if attempt == retries - 1:
				raise last_error
		except gp.GPhoto2Error as e:
			if attempt == retries - 1:
				raise
			sleep(delay)

# upload all files to the host

def uploadCameraFiles(camera):
	files = getCameraFiles(camera)
	
	for path in files:
		info = getFileInfo(camera, path)
	
		folder, name = os.path.split(path)
		target = os.path.join(save_location, label+"_"+name)
		print('Copying frame to', target)
		saveCameraFile(camera, path, target)
	
	return len(files)
	
# upload the latest file to the host

def uploadLatestFile(camera):
	files = getCameraFiles(camera)
	
	latest = 0
	for path in files:
		info = getFileInfo(camera, path)
		if info.file.mtime > latest:
			latest = info.file.mtime
	
	if latest > 0:
		folder, name = os.path.split(path)
		print('Camera file path: {0}/{1}'.format(folder, name))
		target = os.path.join(save_location, label+"_"+name)
		print('Copying frame to', target)
		saveCameraFile(camera, path, target)
		return path
	
# captures a bulb frame and transfers it to the host
# use this method if the camera IS set to 'bulb'
# bulb frames require two triggers (start + stop)

def captureBulbFrame(camera):

	# set eosremoterelease=5 (Immediate)
	# opens the camera shutter
	setConfigValue(camera,'eosremoterelease',5)
	
	shutter = 'open' 
	start_capture = datetime.now()
	end_capture = start_capture + timedelta(seconds=shutterspeed)

	print('Bulb: Opening shutter '+start_capture.strftime('%Y-%m-%d %H:%M:%S:%f'))
	
	while True:
		event_type, event_data = camera.wait_for_event(10)
		sleep(10/1000)
		now = datetime.now()

		# if we have exceeded the exposure time
		if now >= end_capture:
			
			# close the shutter if necessary
			if shutter == 'open':
				print('Bulb: Closing shutter '+now.strftime('%Y-%m-%d %H:%M:%S:%f'))
				setConfigValue(camera,'eosremoterelease',4)
				shutter='closed' 
				
			if event_type == gp.GP_EVENT_TIMEOUT:
				print("Timeout event @ "+str(now))
			elif event_type == gp.GP_EVENT_CAPTURE_COMPLETE:
				print("Capture @ "+str(now))
				break
			elif event_type in (gp.GP_EVENT_FILE_ADDED, gp.GP_EVENT_FOLDER_ADDED):
				print("File or Folder added @ "+str(now))
				break
			
			# if we did not receive a Capture or File Added event !
			if shutter == 'closed' and now > end_capture + timedelta(seconds=5):
				break
		
# recursively looks for files in the 'path' folder 
# returns an array of files on the camera 

def getCameraFiles(camera, path='/'):
    result = []
    # get files in the path
    for name, value in camera.folder_list_files(path):
        result.append(os.path.join(path, name))
    # read folders in the path
    folders = []
    for name, value in camera.folder_list_folders(path):
        folders.append(name)
    # recurse over subfolders
    for name in folders:
        result.extend(getCameraFiles(camera, os.path.join(path, name)))
    return result

# prints a list of all files on the camera to stdout
# list contains file name, size and datetime of frame

def printCameraFiles(camera):
	files = getCameraFiles(camera)
	
	for path in files:
		info = getFileInfo(camera, path)
		print("File:"+path+" size:"+str(info.file.size/1e6)+"Mb time:"+str(info.file.mtime))

# 'path' full name (includes path) of a file on the camera
# returns file info including file size and datetime of frame

def getFileInfo(camera, path):
    folder, name = os.path.split(path)
    return camera.file_get_info(folder, name)

# 'path' full name (includes path) of a file on the camera
# permanently deletes a file from the camera

def deleteCameraFile(camera, path):
    folder, name = os.path.split(path)
    print("Deleting:"+path)
    camera.file_delete(folder, name)

# 'path' full name (includes path) of a file on the camera
# permanently deletes ALL files from the camera

def deleteAllCameraFiles(camera):
	files = getCameraFiles(camera)
	
	for path in files:
		deleteCameraFile(camera,path)

# returns the value of a camera configuration item

def getConfigValue(camera, name):
	# get current camera config
	config = gp.check_result(gp.gp_camera_get_config(camera))
	
	# retrieve config item
	child = gp.check_result(gp.gp_widget_get_child_by_name(config, name))
	value = gp.check_result(gp.gp_widget_get_value(child))
	return str(value)

# iterates through two levels of camera configuration
# prints the camera configuration to stdout 
		
def printCameraConfig(camera):
	config = camera.get_config()
	child_count = config.count_children()
	
	for n in range(child_count):
		child = config.get_child(n)
		label = '{} ({})'.format(child.get_label(), child.get_name())
		print(label)
		child_count_item = child.count_children()
           
		# iterate through second level items in configuration

		for o in range(child_count_item):
			child_item = child.get_child(o)
			label = '    {} ({}) ({}) '.format(child_item.get_label(),
				child_item.get_name(), child_item.get_value())
			print(label)

# name specifies a camera config item (see printCameraConfig)
# value is used to set the config item
# example name=shutterspeed value=1

def setConfigValue(camera, name, value):
	# get current camera config
	config = gp.check_result(gp.gp_camera_get_config(camera))
	
	# retrieve config item to set
	child = gp.check_result(gp.gp_widget_get_child_by_name(config, name))
	
	# prints the set of choices for the config item
	count = gp.check_result(gp.gp_widget_count_choices(child))
	for i in range(count):
		choice = gp.check_result(gp.gp_widget_get_choice(child,i))
		print("Choice:"+str(i)+" Setting:"+choice) 

	# set value
	choice = gp.check_result(gp.gp_widget_get_choice(child, value))
	gp.check_result(gp.gp_widget_set_value(child, choice))
	print("Setting "+name+" to choice "+str(value))
	
	# set config
	gp.check_result(gp.gp_camera_set_config(camera, config))

# returns an array of available shutterspeeds on the camera
# indexed by the choice number e.g. speed[4] = 10 sec implies camera choice = 4

def getShutterSpeeds(camera):
	# get camera camera speed options
	config = gp.check_result(gp.gp_camera_get_config(camera))
	
	# retrieve config item to set
	child = gp.check_result(gp.gp_widget_get_child_by_name(config, 'shutterspeed'))
	
	speeds = []
	# populate speeds array with available choices
	count = gp.check_result(gp.gp_widget_count_choices(child))
	for i in range(count):
		choice = gp.check_result(gp.gp_widget_get_choice(child,i))
		
		fraction = choice.split("/")
		if len(fraction)>=2:
			numerator = float(fraction[0])
			denominator = float(fraction[1])
			speed = numerator / denominator
		elif choice.lower() == 'bulb':
			continue
		else:
			try:
				speed = float(choice)
			except ValueError:
				continue

		speeds.insert(i,speed)
		
	return speeds
	
# returns an array of available iso choices on the camera
# indexed by the choice number e.g. iso[4] = 200 implies camera choice = 4

def getIsoChoices(camera):
	# get camera camera iso options
	config = gp.check_result(gp.gp_camera_get_config(camera))
	
	# retrieve config item to set
	child = gp.check_result(gp.gp_widget_get_child_by_name(config, 'iso'))
	
	iso = []
	# populate iso array with available choices
	count = gp.check_result(gp.gp_widget_count_choices(child))
	for i in range(count):
		choice = gp.check_result(gp.gp_widget_get_choice(child,i))
		iso.insert(i,choice)
		
	return iso
	
def main(argv):
	
	# reference global variables
	global shutterspeed
	global frames
	global label
	global config
	global iso
	global relative_delay
	global absolute_period
	
	# retrieve command line arguments
	try:
		opts, args = getopt.getopt(sys.argv[1:],"hs:f:l:i:c:r:a:",["help","shutterspeed", "frames", "label", "iso", "config", "relative=", "absolute="])
	except getopt.GetoptError as err:
		print(err)
		print('Usage: image_capture.py -h -s <shutterspeed> -f <frames> -l <label> -i <iso> -r <delay> -a <period>')
      
	for ou, arg in opts:
		if ou in ("-h","--help"):
			print('\b\n Usage: image_capture.py -h <help> -s <shutterspeed> -f <frames> -l <label> -c -r <delay> -a <period>\b\n' + \
			'\b\n [-s] Shutterspeed (seconds) e.g. 30' + \
			'\b\n [-f] Number of frames to take e.g. 1' + \
			'\b\n [-l] Label to name image files e.g. Light/Dark/Bias etc' + \
			'\b\n [-i] ISO setting on camera e.g. 800' + \
			'\b\n [-c] Print the camera configuration to stdout' + \
			'\b\n [-r] Relative delay between frames, e.g. 5s or 1m' + \
			'\b\n [-a] Absolute frame period, aligned to wall-clock boundaries, e.g. 1m or 5m')
			sys.exit()
		elif ou in ("-s", "--shutterspeed"):
			shutterspeed = float(arg)
		elif ou in ("-f", "--frames"):
			frames = int(arg)
		elif ou in ("-l", "--label"):
			label = arg
		elif ou in ("-c", "--config"):
			config = True
		elif ou in ("-i", "--iso"):
			iso = int(arg)
		elif ou in ("-r", "--relative"):
			relative_delay = parseInterval(arg)
		elif ou in ("-a", "--absolute"):
			absolute_period = parseInterval(arg)

	if relative_delay is not None and absolute_period is not None:
		print('Specify only one of -r/--relative or -a/--absolute')
		sys.exit(1)
        
	return

#################################################################################################

if __name__ == "__main__":

	sy = main(sys.argv[2:])
	
	print("\b\n Shutterspeed={0} frames={1} label={2} config={3} iso={4} relative_delay={5} absolute_period={6}".format(shutterspeed,frames,label,config,iso,relative_delay,absolute_period))
	
	# kill gphoto2 processes that will prevent access to the gphoto library
	killgphoto2Process()

	# setup callback object to throw exceptions
	callback_obj = gp.check_result(
		gp.gp_log_add_func(gp.GP_LOG_VERBOSE, callback, 'some data'))

	# initialise the camera
	camera = gp.Camera()
	gp.gp_camera_init(camera)
	
	# print configuration 
	if config:
		printCameraConfig(camera)
		
	# if the iso setting needs to be adjusted 
	if iso == 0: # Auto
		setConfigValue(camera,'iso',iso)
	elif iso > 0: 
		options = getIsoChoices(camera)
		print("Options: "+str(options))
		for choice in range(1,len(options)):
			# don't iterate to a smaller choice if we exceed the current speed
			if iso == int(options[choice]):
				break
			elif iso < int(options[choice]):
				print("Camera does not support iso={0}, setting={1} instead!".format(iso,options[choice]))
				break
		setConfigValue(camera,'iso',choice)
	
	# retrieve exposure mode
	autoexposuremode = getConfigValue(camera, 'autoexposuremode')
		
	if autoexposuremode.upper() != 'BULB' and shutterspeed > 30:
		print("Camera must be in Bulb mode for shutterspeed "+str(shutterspeed))
		print("Camera mode is "+autoexposuremode)
		sys.exit()
	
	createFramesFolder()
	
	# repeat 'frames' times
	for f in range(frames):
		waitForFrameTiming(f)
		
		if autoexposuremode.upper() == 'BULB':
			captureBulbFrame(camera)
			sleep(2) # give some time to record the file
		elif autoexposuremode.upper() == 'MANUAL':
			options = getShutterSpeeds(camera)
			
			# assume speeds list is ordered in decreasing speed
			for choice in range(len(options)):
				# don't iterate to a smaller choice if we exceed the current speed
				if shutterspeed == options[choice]:
					break
				elif shutterspeed > options[choice]:
					print("Camera does not support shutterspeed={0}, setting={1} instead!".format(shutterspeed,options[choice]))
					break
			
			setConfigValue(camera,'shutterspeed',choice)
			file_path = captureFrame(camera)
			waitForCapturedFile(camera, file_path)
			target = os.path.join(save_location, label+"_"+file_path.name)
			print('Copying frame to', target)
			saveCameraFile(camera, os.path.join(file_path.folder, file_path.name), target)

	if autoexposuremode.upper() == 'BULB':
		uploadCameraFiles(camera)
	deleteAllCameraFiles(camera)

	# uninstall callback
	del callback_obj

	camera.exit()
	print('DONE')


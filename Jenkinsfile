// https://github.com/Rudd-O/shared-jenkins-libraries
@Library('shared-jenkins-libraries@master') _

def buildCmdline(thisStage, nextStage, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease) {
	if (mySeparateBoot == "yes") {
		mySeparateBoot = "--separate-boot=boot-${pname}.img"
	} else {
		mySeparateBoot = ""
	}
	if (myBuildFrom == "RPMs") {
		myBuildFrom = "--use-prebuilt-rpms=out/${myRelease}/"
	} else {
		myBuildFrom = ""
	}
	if (myLuks == "yes") {
		myLuks = "--luks-password=seed"
	} else {
		myLuks = ""
	}
	if (mySourceBranch != "") {
		mySourceBranch = "--use-branch=${env.SOURCE_BRANCH}"
	}
	def myShortCircuit = "--short-circuit=${thisStage}"
	def myBreakBefore = ""
	if (nextStage != null) {
		myBreakBefore = "--break-before=${nextStage}"
	}
	def program = """
		yumcache="/jenkins/yumcache/${myRelease}"
		mntdir="\$PWD/mnt/${pname}"
		mkdir -p "\$mntdir"
		volsize=10000
		cmd=src/install-fedora-on-zfs
		set -x
		set +e
		ret=0
		ls -l
		sudo \\
			python2 -u "\$cmd" \\
			${myBuildFrom} \\
			${myShortCircuit} \\
			${myBreakBefore} \\
			${mySourceBranch} \\
			${myLuks} \\
			${mySeparateBoot} \\
			--releasever=${myRelease} \\
			--trace-file=/dev/stderr \\
			--workdir="\$mntdir" \\
			--yum-cachedir="\$yumcache" \\
			--host-name="\$HOST_NAME" \\
			--pool-name="${pname}" \\
			--vol-size=\$volsize \\
			--swap-size=256 \\
			--root-password=seed \\
			--chown="\$USER" \\
			--chgrp=`groups | cut -d " " -f 1` \\
			root-${pname}.img >&2
		ret="\$?"
		#>&2 echo ==============Diagnostics==================
		#>&2 sudo zpool list || true
		#>&2 sudo blkid || true
		#>&2 sudo lsblk || true
		#>&2 sudo losetup -la || true
		#>&2 sudo mount || true
		#>&2 echo Return value of program: "\$ret"
		#>&2 echo =========== End Diagnostics ===============
		if [ "\$ret" == "120" ] ; then ret=0 ; fi
		exit "\$ret"
	""".stripIndent().trim()
	return program
}

def runStage(thisStage, allStages, paramShortCircuit, paramBreakBefore, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease, theIt) {
	def thisStageIdx = allStages.findIndexOf{ s -> s == thisStage }
	def nextStage = allStages[thisStageIdx + 1]
	def paramShortCircuitIdx = allStages.findIndexOf{ s -> s == paramShortCircuit }
	def paramBreakBeforeIdx = allStages.findIndexOf{ s -> s == paramBreakBefore }
	def whenCond = ((paramShortCircuit == "" || paramShortCircuitIdx <= thisStageIdx) && (paramBreakBefore == "" || paramBreakBeforeIdx > thisStageIdx))
	def stageName = thisStage.toString().capitalize().replace('_', ' ')
	stage("${stageName} ${theIt.join(' ')}") {
		when (whenCond) {
			def program = buildCmdline(thisStage, nextStage, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease)
			def desc = "============= REPORT ==============\nPool name: ${pname}\nBranch name: ${env.BRANCH_NAME}\nGit hash: ${env.GIT_HASH}\nRelease: ${myRelease}\nBuild from: ${myBuildFrom}\nLUKS: ${myLuks}\nSeparate boot: ${mySeparateBoot}\nSource branch: ${env.SOURCE_BRANCH}\n============= END REPORT =============="
			println "${desc}\n\n" + "Program that will be executed:\n${program}"
			sh(
                            script: program,
                            label: "${stageName} command run"
                        )
		}
	}
}

pipeline {

	agent none

	options {
		checkoutToSubdirectory 'src'
	}

	parameters {
		string defaultValue: "zfs/${currentBuild.projectName}", description: '', name: 'UPSTREAM_PROJECT', trim: true
		string defaultValue: 'master', description: '', name: 'SOURCE_BRANCH', trim: true
		string defaultValue: "grub-zfs-fixer/master", description: '', name: 'GRUB_UPSTREAM_PROJECT', trim: true
		string defaultValue: 'no', description: '', name: 'BUILD_FROM_SOURCE', trim: true
		string defaultValue: 'yes', description: '', name: 'BUILD_FROM_RPMS', trim: true
		string defaultValue: 'seed', description: '', name: 'POOL_NAME', trim: true
		string defaultValue: 'seed.dragonfear', description: '', name: 'HOST_NAME', trim: true
		string defaultValue: 'yes', description: '', name: 'SEPARATE_BOOT', trim: true
		// Having trouble with LUKS being yes on Fedora 25.
		string defaultValue: 'no', description: '', name: 'LUKS', trim: true
		string defaultValue: '', description: 'Stop before this stage.', name: 'BREAK_BEFORE', trim: true
		string defaultValue: '', description: 'Start with this stage.  If this variable is defined, the disk images from prior builds will not be cleaned up prior to short-circuiting to this stage.', name: 'SHORT_CIRCUIT', trim: true
		string defaultValue: '', description: "Which Fedora releases to build for (empty means the job's default).", name: 'RELEASE', trim: true
	}

	stages {
		stage('Preparation') {
			agent { label 'master' }
			steps {
				script{
					if (params.RELEASE == '') {
						env.RELEASE = funcs.loadParameter('RELEASE', '30')
					} else {
						env.RELEASE = params.RELEASE
					}
				}
				script {
					funcs.announceBeginning()
				}
				script {
					env.GIT_HASH = sh (
						script: "cd src && git rev-parse --short HEAD",
						returnStdout: true
					).trim()
					println "Git hash is reported as ${env.GIT_HASH}"
				}
			}
		}
		stage('Setup environment') {
			agent { label 'master' }
			steps {
				script {
					env.GRUB_UPSTREAM_PROJECT = params.GRUB_UPSTREAM_PROJECT
					if (funcs.isUpstreamCause(currentBuild)) {
						def upstreamProject = funcs.getUpstreamProject(currentBuild)
						env.UPSTREAM_PROJECT = upstreamProject
						env.SOURCE_BRANCH = ""
						env.BUILD_FROM_SOURCE = "no"
						env.BUILD_FROM_RPMS = "yes"
					} else {
						env.UPSTREAM_PROJECT = params.UPSTREAM_PROJECT
						env.SOURCE_BRANCH = params.SOURCE_BRANCH
						env.BUILD_FROM_SOURCE = params.BUILD_FROM_SOURCE
						env.BUILD_FROM_RPMS = params.BUILD_FROM_RPMS
					}
					if (env.UPSTREAM_PROJECT == "") {
						currentBuild.result = 'ABORTED'
						error("UPSTREAM_PROJECT must be set to a project containing built ZFS RPMs.")
					}
					if (env.BUILD_FROM_SOURCE == "yes" && env.BUILD_FROM_RPMS == "yes") {
						env.BUILD_FROM = "source RPMs"
					} else if (env.BUILD_FROM_SOURCE == "yes" && env.BUILD_FROM_RPMS == "no") {
						env.BUILD_FROM = "source"
					} else if (env.BUILD_FROM_SOURCE == "no" && env.BUILD_FROM_RPMS == "yes") {
						env.BUILD_FROM = "RPMs"
					} else {
						currentBuild.result = 'ABORTED'
						error("At least one of BUILD_FROM_SOURCE and BUILD_FROM_RPMS must be set to yes.")
					}
					if (env.BUILD_FROM_SOURCE == "yes" && env.SOURCE_BRANCH == "") {
						currentBuild.result = 'ABORTED'
						error("SOURCE_BRANCH must be set when BUILD_FROM_SOURCE is set to yes.")
					}
					env.BUILD_TRIGGER = funcs.describeCause(currentBuild)
					currentBuild.description = "Test of ${env.BUILD_FROM} from source branch ${env.SOURCE_BRANCH} and RPMs from ${env.UPSTREAM_PROJECT}.  ${env.BUILD_TRIGGER}."
				}
			}
		}
		stage('Copy from master') {
			agent { label 'master' }
			when { allOf { not { equals expected: 'NOT_BUILT', actual: currentBuild.result }; equals expected: "", actual: params.SHORT_CIRCUIT } }
			steps {
				sh "rm -rf out"
				copyArtifacts(
					projectName: env.UPSTREAM_PROJECT,
					fingerprintArtifacts: true,
					selector: upstream(fallbackToLastSuccessful: true)
				)
				copyArtifacts(
					projectName: env.GRUB_UPSTREAM_PROJECT,
					fingerprintArtifacts: true,
					selector: upstream(fallbackToLastSuccessful: true)
				)
				sh 'find out/*/*.rpm -type f | sort | grep -v debuginfo | grep -v debugsource | xargs sha256sum | tee /dev/stderr > rpmsums'
				stash includes: 'out/*/*.rpm', name: 'rpms', excludes: '**/*debuginfo*,**/*debugsource*,**/*python*'
				stash includes: 'rpmsums', name: 'rpmsums'
				stash includes: 'src/**', name: 'zfs-fedora-installer'
				script {
					env.DETECTED_RELEASES = sh (
						script: "cd out && ls -1 */zfs-dracut*noarch.rpm | sed 's|/.*||'",
						returnStdout: true
					).trim().replace("\n", ' ')
					println "The detected releases are ${env.DETECTED_RELEASES}"
					if (params.RELEASE == '') {
						println "Overriding releases ${env.RELEASE} with detected releases ${env.DETECTED_RELEASES}"
						env.RELEASE = env.DETECTED_RELEASES
					}
				}
			}
		}
		stage('Serialize') {
			agent { label 'master' }
			when { not { equals expected: 'NOT_BUILT', actual: currentBuild.result } }
			failFast true
			steps {
				script {
					def axisList = [
						env.RELEASE.split(' '),
						env.BUILD_FROM.split(' '),
						params.LUKS.split(' '),
						params.SEPARATE_BOOT.split(' '),
					]
					def task = {
						def myRelease = it[0]
						def myBuildFrom = it[1]
						def myLuks = it[2]
						def mySeparateBoot = it[3]
						def pname = "${env.POOL_NAME}_${env.BRANCH_NAME}_${env.BUILD_NUMBER}_${env.GIT_HASH}_${myRelease}_${myBuildFrom}_${myLuks}_${mySeparateBoot}"
						def mySourceBranch = ""
						if (env.SOURCE_BRANCH != "") {
							mySourceBranch = env.SOURCE_BRANCH
						}
						return node('fedorazfs') {
								stage("Unstash RPMs ${it.join(' ')}") {
									when (params.SHORT_CIRCUIT == "") {
									timeout(time: 10, unit: 'MINUTES') {
										sh 'find out/*/*.rpm -type f | sort | grep -v debuginfo | grep -v debugsource | grep -v python | xargs sha256sum | tee /dev/stderr > local-rpmsums'
										unstash "rpmsums"
										def needsunstash = sh (
											script: '''
											set +e ; set -x
											output=$(diff -Naur local-rpmsums rpmsums 2>&1)
											if [ "$?" = "0" ]
											then
												echo MATCH
											else
												echo "$output" >&2
											fi
											''',
											returnStdout: true
										).trim()
										if (needsunstash != "MATCH") {
											sh 'rm -rf -- out/'
											unstash "rpms"
										}
									}
                                                                        }
								}
								stage("Unstash ${it.join(' ')}") {
									when (params.SHORT_CIRCUIT == "") {
										unstash "zfs-fedora-installer"
									}
								}
								lock("activatezfs") {
									stage("Activate ZFS ${it.join(' ')}") {
										when (params.SHORT_CIRCUIT == "") {
											timeout(time: 10, unit: 'MINUTES') {
												def program = '''
													deps="rsync rpm-build e2fsprogs dosfstools cryptsetup qemu gdisk python2"
													rpm -q \$deps || sudo dnf install -qy \$deps
												'''.stripIndent().trim()
												sh program
												sh 'sudo src/activate-zfs-in-qubes-vm out/'
												sh 'if test -f /usr/sbin/setenforce ; then sudo setenforce 0 || exit $? ; fi'
											}
                                                                        	}
									}
                                                                }
								stage("Remove old image ${it.join(' ')}") {
									when (params.SHORT_CIRCUIT == "") {
										sh "rm -rf root-${pname}.img boot-${pname}.img ${pname}.log"
									}
								}
								timeout(60) {
								runStage("beginning",
									 ["beginning", "reload_chroot", "bootloader_install", "boot_to_test_non_hostonly", "boot_to_test_hostonly"],
									 params.SHORT_CIRCUIT, params.BREAK_BEFORE, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease, it)
                                                                }
								timeout(15) {
								runStage("reload_chroot",
                                                                         ["beginning", "reload_chroot", "bootloader_install", "boot_to_test_non_hostonly", "boot_to_test_hostonly"],
									 params.SHORT_CIRCUIT, params.BREAK_BEFORE, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease, it)
                                                                }
								timeout(30) {
								runStage("bootloader_install",
                                                                         ["beginning", "reload_chroot", "bootloader_install", "boot_to_test_non_hostonly", "boot_to_test_hostonly"],
                                                                         params.SHORT_CIRCUIT, params.BREAK_BEFORE, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease, it)
                                                                }
								timeout(30) {
								runStage("boot_to_test_non_hostonly",
                                                                         ["beginning", "reload_chroot", "bootloader_install", "boot_to_test_non_hostonly", "boot_to_test_hostonly"],
                                                                         params.SHORT_CIRCUIT, params.BREAK_BEFORE, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease, it)
                                                                }
								timeout(30) {
								runStage("boot_to_test_hostonly",
                                                                         ["beginning", "reload_chroot", "bootloader_install", "boot_to_test_non_hostonly", "boot_to_test_hostonly"],
                                                                         params.SHORT_CIRCUIT, params.BREAK_BEFORE, pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease, it)
                                                                }
						}
					}
					def tasks = funcs.combo(task, axisList)
					tasks.each {
                                            stage(it.key) {
                                                script {
                                                    it.value
                                                }
                                            }
                                        }
				}
			}
		}
	}
	post {
		always {
			node('master') {
				script {
					funcs.announceEnd(currentBuild.currentResult)
				}
			}
		}
	}
}
